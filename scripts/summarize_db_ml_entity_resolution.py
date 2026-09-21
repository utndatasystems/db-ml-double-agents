#!/usr/bin/env python3
"""Summarize DB-reviewer <-> ML-author overlap using persistent DBLP identities.

This script combines the outputs of:

    enrich_reviewers_with_dblp.py  -> data/entity_resolution.sqlite
    enrich_ml_with_dblp.py         -> data/ml_entity_resolution.sqlite

Outputs
-------
    data/db_ml_resolved_people.csv
        One row per persistent DBLP PID that occurs on both the DB-reviewer
        side and the ML-publication side.

    data/db_ml_entity_resolution_summary.txt
        Compact reproducible count summary suitable for sanity checking.

Important interpretation
------------------------
Only DB reviewer name groups with an automatically resolved DBLP PID are used
for high-confidence PID overlap. Therefore the resulting person count is a
HIGH-CONFIDENCE LOWER BOUND, not necessarily the full DB<->ML overlap.

Example
-------
    python scripts/summarize_db_ml_entity_resolution.py --data-dir .\\data
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

DB_ER_DEFAULT = "entity_resolution.sqlite"
ML_ER_DEFAULT = "ml_entity_resolution.sqlite"
PEOPLE_OUT = "db_ml_resolved_people.csv"
SUMMARY_OUT = "db_ml_entity_resolution_summary.txt"


def normalize_name(value: Any) -> str:
    if value is None:
        return ""
    s = unicodedata.normalize("NFKD", str(value))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def require_schema(
    conn: sqlite3.Connection,
    db_label: str,
    requirements: dict[str, set[str]],
) -> None:
    problems: list[str] = []
    for table, required_cols in requirements.items():
        if not table_exists(conn, table):
            problems.append(f"missing table {table!r}")
            continue
        cols = table_columns(conn, table)
        missing = required_cols - cols
        if missing:
            problems.append(
                f"table {table!r} missing column(s): {', '.join(sorted(missing))}"
            )
    if problems:
        raise RuntimeError(
            f"{db_label} schema mismatch: " + "; ".join(problems)
        )


def json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(x) for x in value if x not in (None, "")]
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [str(x) for x in parsed if x not in (None, "")]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def pipe(values: Iterable[Any]) -> str:
    vals = sorted(
        {str(v).strip() for v in values if v is not None and str(v).strip()},
        key=str.casefold,
    )
    return " | ".join(vals)


def int_pipe(values: Iterable[Any]) -> str:
    ints: set[int] = set()
    other: set[str] = set()
    for value in values:
        if value in (None, ""):
            continue
        try:
            ints.add(int(value))
        except (TypeError, ValueError):
            other.add(str(value))
    parts = [str(x) for x in sorted(ints)] + sorted(other)
    return " | ".join(parts)


def pct(n: int, d: int) -> str:
    if not d:
        return "n/a"
    return f"{100.0 * n / d:.1f}%"


def load_db_side(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return (resolved people by PID, resolution records by DB name group)."""
    require_schema(
        conn,
        "DB reviewer enrichment",
        {
            "reviewer_observations": {
                "name_key",
                "name",
                "venue",
                "year",
                "role",
                "affiliation",
            },
            "name_resolution": {
                "name_key",
                "representative_name",
                "status",
                "resolved_dblp_pid",
                "resolved_dblp_name",
                "best_orcid",
                "best_orcid_source",
            },
        },
    )

    resolution: dict[str, dict[str, Any]] = {}
    for row in conn.execute("""
        SELECT name_key, representative_name, status,
               resolved_dblp_pid, resolved_dblp_name,
               best_orcid, best_orcid_source
        FROM name_resolution
        """):
        resolution[row[0]] = {
            "name_key": row[0],
            "representative_name": row[1],
            "status": row[2],
            "pid": row[3],
            "resolved_name": row[4],
            "best_orcid": row[5],
            "best_orcid_source": row[6],
        }

    by_pid: dict[str, dict[str, Any]] = {}

    for row in conn.execute("""
        SELECT
            r.name_key,
            r.representative_name,
            r.status,
            r.resolved_dblp_pid,
            r.resolved_dblp_name,
            r.best_orcid,
            r.best_orcid_source,
            o.name,
            o.venue,
            o.year,
            o.role,
            o.review_round,
            o.review_cycle,
            o.affiliation
        FROM name_resolution r
        JOIN reviewer_observations o ON o.name_key=r.name_key
        WHERE r.resolved_dblp_pid IS NOT NULL
        ORDER BY r.resolved_dblp_pid, o.observation_id
        """):
        (
            name_key,
            representative,
            status,
            pid,
            resolved_name,
            best_orcid,
            best_orcid_source,
            observed_name,
            venue,
            year,
            role,
            review_round,
            review_cycle,
            affiliation,
        ) = row

        g = by_pid.setdefault(
            pid,
            {
                "pid": pid,
                "resolved_names": set(),
                "db_name_keys": set(),
                "db_name_variants": set(),
                "db_resolution_statuses": set(),
                "db_venues": set(),
                "db_years": set(),
                "db_roles": set(),
                "db_rounds": set(),
                "db_review_cycles": set(),
                "db_affiliations": set(),
                "db_observation_count": 0,
                "db_unique_venue_year": set(),
                "orcids": defaultdict(set),
            },
        )
        if resolved_name:
            g["resolved_names"].add(resolved_name)
        if representative:
            g["db_name_variants"].add(representative)
        if observed_name:
            g["db_name_variants"].add(observed_name)
        g["db_name_keys"].add(name_key)
        g["db_resolution_statuses"].add(status)
        if venue:
            g["db_venues"].add(venue)
        if year is not None:
            g["db_years"].add(year)
        if role:
            g["db_roles"].add(role)
        if review_round:
            g["db_rounds"].add(review_round)
        if review_cycle:
            g["db_review_cycles"].add(review_cycle)
        if affiliation:
            g["db_affiliations"].add(affiliation)
        g["db_observation_count"] += 1
        if venue and year is not None:
            g["db_unique_venue_year"].add((venue, year))
        if best_orcid:
            g["orcids"][best_orcid].add(best_orcid_source or "db_best_orcid")

    return by_pid, resolution


def load_ml_side(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """Return (resolved people by PID, total occurrences, resolved occurrences)."""
    require_schema(
        conn,
        "ML enrichment",
        {
            "author_occurrences": {
                "source_db",
                "paper_uid",
                "venue",
                "year",
                "paper_title",
                "local_author_name",
                "dblp_author_name",
                "publication_orcid",
                "dblp_pid",
                "dblp_canonical_name",
                "person_orcids_json",
                "best_orcid",
                "best_orcid_source",
                "resolution_status",
            },
        },
    )

    by_pid: dict[str, dict[str, Any]] = {}
    total_occurrences = 0
    resolved_occurrences = 0

    for row in conn.execute("""
        SELECT
            source_db, paper_uid, venue, year, paper_title,
            local_author_position, local_author_name,
            dblp_key, dblp_author_name,
            publication_orcid, dblp_pid, dblp_canonical_name,
            person_orcids_json, best_orcid, best_orcid_source,
            resolution_status
        FROM author_occurrences
        ORDER BY venue, year, source_db, paper_uid, local_author_position
        """):
        (
            source_db,
            paper_uid,
            venue,
            year,
            _title,
            _author_position,
            local_name,
            _dblp_key,
            dblp_signature,
            publication_orcid,
            pid,
            canonical_name,
            person_orcids_json,
            best_orcid,
            best_orcid_source,
            _resolution_status,
        ) = row

        total_occurrences += 1
        if not pid:
            continue
        resolved_occurrences += 1

        g = by_pid.setdefault(
            pid,
            {
                "pid": pid,
                "canonical_names": set(),
                "ml_name_variants": set(),
                "ml_dblp_signatures": set(),
                "ml_venues": set(),
                "ml_years": set(),
                "papers": set(),
                "ml_author_occurrence_count": 0,
                "orcids": defaultdict(set),
            },
        )
        if canonical_name:
            g["canonical_names"].add(canonical_name)
        if local_name:
            g["ml_name_variants"].add(local_name)
        if dblp_signature:
            g["ml_dblp_signatures"].add(dblp_signature)
        if venue:
            g["ml_venues"].add(venue)
        if year is not None:
            g["ml_years"].add(year)
        g["papers"].add((source_db, paper_uid))
        g["ml_author_occurrence_count"] += 1

        for oc in json_list(person_orcids_json):
            g["orcids"][oc].add("ml_dblp_person_record")
        if publication_orcid:
            g["orcids"][publication_orcid].add("ml_publication_orcid")
        if best_orcid:
            g["orcids"][best_orcid].add(best_orcid_source or "ml_best_orcid")

    return by_pid, total_occurrences, resolved_occurrences


def choose_canonical_name(db: dict[str, Any], ml: dict[str, Any]) -> str:
    candidates = list(db.get("resolved_names", set())) + list(
        ml.get("canonical_names", set())
    )
    if not candidates:
        candidates = list(db.get("db_name_variants", set())) + list(
            ml.get("ml_name_variants", set())
        )
    if not candidates:
        return db.get("pid", ml.get("pid", ""))
    return sorted(
        candidates,
        key=lambda x: (len(normalize_name(x).split()), len(x)),
        reverse=True,
    )[0]


def consolidate_orcids(
    db: dict[str, Any], ml: dict[str, Any]
) -> tuple[str, str, str]:
    evidence: dict[str, set[str]] = defaultdict(set)
    for side in (db, ml):
        for oc, sources in side.get("orcids", {}).items():
            evidence[oc].update(sources)

    if not evidence:
        return "", "", "none"

    all_orcids = sorted(evidence)
    sources = [f"{oc}:" + ",".join(sorted(evidence[oc])) for oc in all_orcids]
    status = "unique" if len(all_orcids) == 1 else "conflict_multiple_orcids"
    return " | ".join(all_orcids), " | ".join(sources), status


def write_people_csv(
    path: Path,
    db_by_pid: dict[str, dict[str, Any]],
    ml_by_pid: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    overlap_pids = sorted(set(db_by_pid) & set(ml_by_pid))
    rows: list[dict[str, Any]] = []

    for pid in overlap_pids:
        db = db_by_pid[pid]
        ml = ml_by_pid[pid]
        orcids, orcid_sources, orcid_status = consolidate_orcids(db, ml)
        ml_years = [int(y) for y in ml["ml_years"] if str(y).isdigit()]

        rows.append(
            {
                "dblp_pid": pid,
                "canonical_name": choose_canonical_name(db, ml),
                "orcids": orcids,
                "orcid_status": orcid_status,
                "orcid_evidence": orcid_sources,
                "db_name_variants": pipe(db["db_name_variants"]),
                "db_name_keys": pipe(db["db_name_keys"]),
                "db_resolution_statuses": pipe(db["db_resolution_statuses"]),
                "db_venues": pipe(db["db_venues"]),
                "db_years": int_pipe(db["db_years"]),
                "db_roles": pipe(db["db_roles"]),
                "db_affiliations": pipe(db["db_affiliations"]),
                "db_observation_count": db["db_observation_count"],
                "db_unique_venue_year_count": len(db["db_unique_venue_year"]),
                "ml_name_variants": pipe(ml["ml_name_variants"]),
                "ml_dblp_signatures": pipe(ml["ml_dblp_signatures"]),
                "ml_venues": pipe(ml["ml_venues"]),
                "ml_years": int_pipe(ml["ml_years"]),
                "ml_paper_count": len(ml["papers"]),
                "ml_author_occurrence_count": ml["ml_author_occurrence_count"],
                "first_ml_year": min(ml_years) if ml_years else "",
                "last_ml_year": max(ml_years) if ml_years else "",
                "resolution_evidence": "same_persistent_dblp_pid",
            }
        )

    fields = [
        "dblp_pid",
        "canonical_name",
        "orcids",
        "orcid_status",
        "orcid_evidence",
        "db_name_variants",
        "db_name_keys",
        "db_resolution_statuses",
        "db_venues",
        "db_years",
        "db_roles",
        "db_affiliations",
        "db_observation_count",
        "db_unique_venue_year_count",
        "ml_name_variants",
        "ml_dblp_signatures",
        "ml_venues",
        "ml_years",
        "ml_paper_count",
        "ml_author_occurrence_count",
        "first_ml_year",
        "last_ml_year",
        "resolution_evidence",
    ]

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    ap.add_argument("--output-dir", type=Path, default=None)
    ap.add_argument(
        "--db-er",
        type=Path,
        default=None,
        help="Reviewer enrichment SQLite (default: DATA_DIR/entity_resolution.sqlite)",
    )
    ap.add_argument(
        "--ml-er",
        type=Path,
        default=None,
        help="ML enrichment SQLite (default: DATA_DIR/ml_entity_resolution.sqlite)",
    )
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    output_dir = (args.output_dir or args.data_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = (args.db_er or (data_dir / DB_ER_DEFAULT)).resolve()
    ml_path = (args.ml_er or (data_dir / ML_ER_DEFAULT)).resolve()

    for label, path in (
        ("DB reviewer enrichment", db_path),
        ("ML enrichment", ml_path),
    ):
        if not path.exists():
            print(f"ERROR: {label} database not found: {path}", file=sys.stderr)
            return 2

    print(f"DB reviewer enrichment: {db_path}")
    print(f"ML enrichment:          {ml_path}")

    db_conn = sqlite3.connect(db_path)
    ml_conn = sqlite3.connect(ml_path)
    try:
        print("\n[1/3] Loading resolved DB reviewer identities...")
        db_by_pid, db_resolution = load_db_side(db_conn)
        total_db_name_groups = len(db_resolution)
        resolved_db_name_groups = sum(
            1 for x in db_resolution.values() if x.get("pid")
        )
        print(f"  DB name groups: {total_db_name_groups}")
        print(f"  DB name groups with resolved PID: {resolved_db_name_groups}")
        print(f"  Distinct resolved DB PIDs: {len(db_by_pid)}")

        print("\n[2/3] Loading resolved ML author identities...")
        ml_by_pid, total_ml_occ, resolved_ml_occ = load_ml_side(ml_conn)
        print(f"  ML author occurrences: {total_ml_occ}")
        print(f"  ML author occurrences with PID: {resolved_ml_occ}")
        print(f"  Distinct resolved ML PIDs: {len(ml_by_pid)}")

        print("\n[3/3] Building one-row-per-person DB<->ML PID overlap...")
        people_path = output_dir / PEOPLE_OUT
        people_rows = write_people_csv(people_path, db_by_pid, ml_by_pid)
        overlap_with_orcid = sum(1 for row in people_rows if row["orcids"])

        print(
            f"  High-confidence distinct PID-overlap people: {len(people_rows)}"
        )
        print(
            f"  PID-overlap people with ORCID evidence:       {overlap_with_orcid}"
        )

        summary_lines = [
            "DB <-> ML entity-resolution summary",
            "=================================",
            "",
            f"DB reviewer name groups: {total_db_name_groups}",
            f"DB reviewer name groups with resolved DBLP PID: {resolved_db_name_groups} ({pct(resolved_db_name_groups, total_db_name_groups)})",
            f"Distinct resolved DB reviewer PIDs: {len(db_by_pid)}",
            "",
            f"ML author occurrences: {total_ml_occ}",
            f"ML author occurrences with resolved DBLP PID: {resolved_ml_occ} ({pct(resolved_ml_occ, total_ml_occ)})",
            f"Distinct resolved ML PIDs: {len(ml_by_pid)}",
            "",
            f"Distinct high-confidence DB<->ML PID-overlap people: {len(people_rows)}",
            f"PID overlap among distinct resolved DB PIDs: {len(people_rows)}/{len(db_by_pid)} ({pct(len(people_rows), len(db_by_pid))})",
            f"PID-overlap people with ORCID evidence: {overlap_with_orcid}",
            "",
            "Interpretation:",
            "- the same persistent DBLP PID is the high-confidence identity evidence used for DB<->ML overlap.",
            "- the distinct PID-overlap count is a lower bound because unresolved/ambiguous DB reviewer names and unresolved ML author occurrences are excluded.",
        ]

        summary_path = output_dir / SUMMARY_OUT
        summary_path.write_text(
            "\n".join(summary_lines) + "\n", encoding="utf-8"
        )

        print("\n" + "\n".join(summary_lines))
        print("\nOutputs")
        print("-------")
        print(f"Resolved people: {people_path}")
        print(f"Summary:         {summary_path}")
        return 0
    finally:
        db_conn.close()
        ml_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
