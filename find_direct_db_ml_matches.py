#!/usr/bin/env python3
"""Find direct DB-community -> ML-paper overlaps using exact normalized names only.

Expected input databases (in --data-dir):
  sigmod_reviewers.sqlite
  vldb_reviewers.sqlite
  cidr_reviewers.sqlite
  icde_reviewers.sqlite
  edbt_reviewers.sqlite
  neurips_papers.sqlite
  icml_papers.sqlite
  iclr_papers.sqlite

Outputs (in --output-dir):
  direct_name_matches.csv
  direct_people_summary.csv

No fuzzy/entity matching is performed. Names are only normalized for case,
Unicode accents, punctuation, and whitespace before exact equality testing.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

REVIEWER_DBS = [
    "sigmod_reviewers.sqlite",
    "vldb_reviewers.sqlite",
    "cidr_reviewers.sqlite",
    "icde_reviewers.sqlite",
    "edbt_reviewers.sqlite",
]

ML_DBS = [
    "neurips_papers.sqlite",
    "icml_papers.sqlite",
    "iclr_papers.sqlite",
]


def normalize_name(name: str | None) -> str:
    """Conservative normalization for exact matching only."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def first_existing(cols: set[str], *candidates: str) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def fetch_reviewer_rows(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "reviewer_records" not in tables:
            raise RuntimeError(f"{db_path.name}: reviewer_records table not found")

        cols = table_columns(con, "reviewer_records")
        name_col = first_existing(cols, "name", "reviewer_name")
        year_col = first_existing(cols, "year", "conference_year")
        venue_col = first_existing(cols, "venue")
        role_col = first_existing(cols, "role")
        affiliation_col = first_existing(cols, "affiliation")

        if not name_col:
            raise RuntimeError(f"{db_path.name}: no reviewer name column found")

        wanted = [name_col]
        for c in [year_col, venue_col, role_col, affiliation_col]:
            if c and c not in wanted:
                wanted.append(c)

        sql = "SELECT " + ", ".join(wanted) + " FROM reviewer_records"
        out = []
        for row in con.execute(sql):
            name = row[name_col]
            key = normalize_name(name)
            if not key:
                continue
            out.append({
                "db_source": db_path.name,
                "db_name": name,
                "db_name_key": key,
                "db_year": row[year_col] if year_col else None,
                "db_venue": row[venue_col] if venue_col else db_path.stem.split("_")[0].upper(),
                "db_role": row[role_col] if role_col else None,
                "db_affiliation": row[affiliation_col] if affiliation_col else None,
            })
        return out
    finally:
        con.close()


def fetch_ml_rows(db_path: Path) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"papers", "paper_authors"}.issubset(tables):
            raise RuntimeError(f"{db_path.name}: expected papers and paper_authors tables")

        pcols = table_columns(con, "papers")
        acols = table_columns(con, "paper_authors")

        paper_uid = first_existing(pcols, "paper_uid", "id")
        author_uid = first_existing(acols, "paper_uid", "paper_id")
        author_name = first_existing(acols, "author_name", "name")
        author_pos = first_existing(acols, "author_position", "position")
        title_col = first_existing(pcols, "title")
        venue_col = first_existing(pcols, "venue")
        year_col = first_existing(pcols, "year")
        url_col = first_existing(pcols, "source_url", "url")

        if not paper_uid or not author_uid or not author_name or not title_col:
            raise RuntimeError(f"{db_path.name}: unexpected papers/paper_authors schema")

        select_parts = [
            f"a.{author_name} AS author_name",
            f"p.{title_col} AS title",
        ]
        select_parts.append(f"a.{author_pos} AS author_position" if author_pos else "NULL AS author_position")
        select_parts.append(f"p.{venue_col} AS venue" if venue_col else "NULL AS venue")
        select_parts.append(f"p.{year_col} AS year" if year_col else "NULL AS year")
        select_parts.append(f"p.{url_col} AS source_url" if url_col else "NULL AS source_url")

        sql = f"""
            SELECT {', '.join(select_parts)}
            FROM paper_authors a
            JOIN papers p ON a.{author_uid} = p.{paper_uid}
        """

        out = []
        for row in con.execute(sql):
            name = row["author_name"]
            key = normalize_name(name)
            if not key:
                continue
            out.append({
                "ml_source": db_path.name,
                "ml_author_name": name,
                "ml_name_key": key,
                "ml_venue": row["venue"] or db_path.stem.split("_")[0].upper(),
                "ml_year": row["year"],
                "paper_title": row["title"],
                "author_position": row["author_position"],
                "paper_url": row["source_url"],
            })
        return out
    finally:
        con.close()


def safe(v):
    return "" if v is None else v


def main() -> int:
    parser = argparse.ArgumentParser(description="Exact normalized-name overlap between DB reviewers and ML paper authors.")
    parser.add_argument("--data-dir", default="data", help="Directory containing the SQLite databases (default: data)")
    parser.add_argument("--output-dir", default="data", help="Directory for CSV outputs (default: data)")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    missing = [f for f in REVIEWER_DBS + ML_DBS if not (data_dir / f).exists()]
    if missing:
        print("Missing expected database file(s):")
        for f in missing:
            print(f"  - {data_dir / f}")
        return 2

    reviewer_rows = []
    for fname in REVIEWER_DBS:
        rows = fetch_reviewer_rows(data_dir / fname)
        reviewer_rows.extend(rows)
        print(f"Loaded {len(rows):6d} reviewer records from {fname}")

    ml_rows = []
    for fname in ML_DBS:
        rows = fetch_ml_rows(data_dir / fname)
        ml_rows.extend(rows)
        print(f"Loaded {len(rows):6d} paper-author rows from {fname}")

    # Index ML author occurrences by exact normalized name.
    ml_by_key: dict[str, list[dict]] = defaultdict(list)
    for r in ml_rows:
        ml_by_key[r["ml_name_key"]].append(r)

    reviewer_keys = {r["db_name_key"] for r in reviewer_rows}
    overlap_keys = reviewer_keys.intersection(ml_by_key)

    # Preserve every DB-service x ML-paper occurrence. This is intentionally raw.
    matches = []
    for db in reviewer_rows:
        for ml in ml_by_key.get(db["db_name_key"], []):
            matches.append({**db, **ml})

    matches_path = out_dir / "direct_name_matches.csv"
    match_fields = [
        "db_name_key",
        "db_name",
        "db_venue",
        "db_year",
        "db_role",
        "db_affiliation",
        "db_source",
        "ml_author_name",
        "ml_venue",
        "ml_year",
        "paper_title",
        "author_position",
        "paper_url",
        "ml_source",
    ]
    with matches_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=match_fields)
        w.writeheader()
        for row in sorted(matches, key=lambda r: (r["db_name_key"], str(r["ml_year"]), r["paper_title"])):
            w.writerow({k: safe(row.get(k)) for k in match_fields})

    # Compact one-row-per-normalized-name summary.
    db_by_key: dict[str, list[dict]] = defaultdict(list)
    for r in reviewer_rows:
        if r["db_name_key"] in overlap_keys:
            db_by_key[r["db_name_key"]].append(r)

    summary_rows = []
    for key in sorted(overlap_keys):
        dbs = db_by_key[key]
        mls = ml_by_key[key]
        canonical_db_name = max((r["db_name"] for r in dbs), key=len)
        ml_names = sorted({r["ml_author_name"] for r in mls})
        db_venues = sorted({str(r["db_venue"]) for r in dbs if r["db_venue"]})
        ml_venues = sorted({str(r["ml_venue"]) for r in mls if r["ml_venue"]})
        affiliations = sorted({str(r["db_affiliation"]) for r in dbs if r["db_affiliation"]})
        paper_ids = {(r["ml_venue"], r["ml_year"], r["paper_title"]) for r in mls}
        years = [int(r["ml_year"]) for r in mls if str(r["ml_year"]).isdigit()]

        summary_rows.append({
            "name_key": key,
            "db_name": canonical_db_name,
            "ml_name_variants": " | ".join(ml_names),
            "db_venues": " | ".join(db_venues),
            "db_service_records": len(dbs),
            "db_affiliations": " | ".join(affiliations),
            "ml_venues": " | ".join(ml_venues),
            "ml_paper_count": len(paper_ids),
            "first_ml_year": min(years) if years else "",
            "last_ml_year": max(years) if years else "",
        })

    summary_path = out_dir / "direct_people_summary.csv"
    summary_fields = [
        "name_key",
        "db_name",
        "ml_name_variants",
        "db_venues",
        "db_service_records",
        "db_affiliations",
        "ml_venues",
        "ml_paper_count",
        "first_ml_year",
        "last_ml_year",
    ]
    with summary_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=summary_fields)
        w.writeheader()
        w.writerows(summary_rows)

    print()
    print("Exact normalized-name overlap")
    print("-----------------------------")
    print(f"Distinct DB reviewer name keys: {len(reviewer_keys)}")
    print(f"Distinct names also found in ML papers: {len(overlap_keys)}")
    print(f"Raw DB-service x ML-paper match rows: {len(matches)}")
    print()
    print(f"Wrote: {matches_path}")
    print(f"Wrote: {summary_path}")
    print()
    print("Important: exact full-name equality is only a candidate match.")
    print("Common names can still be false positives; inspect ambiguous/common names manually.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
