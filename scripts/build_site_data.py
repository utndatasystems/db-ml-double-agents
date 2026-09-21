#!/usr/bin/env python3
"""Build static website data from the DB/ML entity-resolution outputs.

Required inputs in --data-dir:
    entity_resolution.sqlite
    ml_entity_resolution.sqlite

Outputs in --output-dir:
    site_data.json
    site_data.js

With no arguments, inputs default to ../data and outputs to ../website/data
relative to this script.

The resulting site_data.js assigns window.CROSSOVER_DATA so the static website
can be opened without a backend or fetch API. site_data.json is emitted as a
plain reusable artifact for future frontends/APIs.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DB_ER = "entity_resolution.sqlite"
ML_ER = "ml_entity_resolution.sqlite"
SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "website" / "data"


def clean(value: Any) -> str:
    return "" if value is None else re.sub(r"\s+", " ", str(value).strip())


def normalize_name(value: Any) -> str:
    s = unicodedata.normalize("NFKD", clean(value))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [clean(x) for x in value if clean(x)]
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [clean(x) for x in parsed if clean(x)]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def unique(values: Iterable[Any]) -> list[str]:
    return sorted({clean(x) for x in values if clean(x)}, key=str.casefold)


def require_tables(
    conn: sqlite3.Connection, names: Iterable[str], label: str
) -> None:
    have = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = [name for name in names if name not in have]
    if missing:
        raise RuntimeError(f"{label} is missing table(s): {', '.join(missing)}")


def coverage(values: dict[str, set[int]]) -> dict[str, dict[str, int]]:
    result = {}
    for venue, ys in sorted(values.items()):
        if ys:
            result[venue] = {"min": min(ys), "max": max(ys)}
    return result


def choose_name(names: Iterable[str], fallback: str) -> str:
    vals = unique(names)
    if not vals:
        return fallback
    return sorted(
        vals,
        key=lambda x: (len(normalize_name(x).split()), len(x)),
        reverse=True,
    )[0]


def load_confirmed(
    db_conn: sqlite3.Connection,
    ml_conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    require_tables(
        db_conn,
        ["name_resolution", "reviewer_observations", "dblp_people"],
        "DB enrichment DB",
    )
    require_tables(
        ml_conn,
        ["author_occurrences", "ml_papers", "dblp_people"],
        "ML enrichment DB",
    )

    db: dict[str, dict[str, Any]] = {}
    db_coverage: dict[str, set[int]] = defaultdict(set)

    query = """
        SELECT r.resolved_dblp_pid, r.resolved_dblp_name, r.best_orcid,
               r.best_orcid_source, r.representative_name,
               o.name, o.venue, o.year, o.role, o.review_round,
               o.review_cycle, o.affiliation, o.source_url
        FROM name_resolution r
        JOIN reviewer_observations o ON o.name_key=r.name_key
        WHERE r.resolved_dblp_pid IS NOT NULL
        ORDER BY r.resolved_dblp_pid, o.year, o.venue
    """
    for row in db_conn.execute(query):
        (
            pid,
            resolved_name,
            best_orcid,
            _best_orcid_source,
            representative,
            observed_name,
            venue,
            year,
            role,
            review_round,
            review_cycle,
            affiliation,
            source_url,
        ) = row
        item = db.setdefault(
            pid,
            {
                "pid": pid,
                "names": set(),
                "aliases": set(),
                "affiliations": set(),
                "venues": set(),
                "years": set(),
                "roles": set(),
                "orcids": set(),
                "events": defaultdict(
                    lambda: {
                        "rounds": set(),
                        "cycles": set(),
                        "affiliations": set(),
                        "sources": set(),
                        "observations": 0,
                    }
                ),
            },
        )
        item["names"].update(x for x in (resolved_name, representative) if x)
        if observed_name:
            item["aliases"].add(observed_name)
        if affiliation:
            item["affiliations"].add(affiliation)
        if venue:
            item["venues"].add(venue)
        if role:
            item["roles"].add(role)
        if year is not None:
            item["years"].add(int(year))
            if venue:
                db_coverage[venue].add(int(year))
        if best_orcid:
            item["orcids"].add(best_orcid)
        event_key = (
            venue or "",
            int(year) if year is not None else None,
            role or "",
        )
        ev = item["events"][event_key]
        ev["observations"] += 1
        if review_round:
            ev["rounds"].add(review_round)
        if review_cycle:
            ev["cycles"].add(review_cycle)
        if affiliation:
            ev["affiliations"].add(affiliation)
        if source_url:
            ev["sources"].add(source_url)

    for (
        pid,
        canonical,
        aliases_json,
        affiliations_json,
        orcids_json,
    ) in db_conn.execute(
        "SELECT dblp_pid, canonical_name, aliases_json, affiliations_json, verified_orcids_json FROM dblp_people"
    ):
        if pid not in db:
            continue
        item = db[pid]
        if canonical:
            item["names"].add(canonical)
        item["aliases"].update(json_list(aliases_json))
        item["affiliations"].update(json_list(affiliations_json))
        item["orcids"].update(json_list(orcids_json))

    ml: dict[str, dict[str, Any]] = {}
    ml_coverage: dict[str, set[int]] = defaultdict(set)

    query = """
        SELECT a.dblp_pid, a.dblp_canonical_name, a.local_author_name,
               a.dblp_author_name, a.person_orcids_json, a.publication_orcid,
               a.best_orcid, a.person_affiliations_json,
               a.source_db, a.paper_uid, a.venue, a.year, a.paper_title,
               a.local_author_position, p.source_url
        FROM author_occurrences a
        LEFT JOIN ml_papers p
          ON p.source_db=a.source_db AND p.paper_uid=a.paper_uid
        WHERE a.dblp_pid IS NOT NULL
        ORDER BY a.dblp_pid, a.year, a.venue, a.paper_title
    """
    for row in ml_conn.execute(query):
        (
            pid,
            canonical_name,
            local_name,
            dblp_author_name,
            person_orcids_json,
            publication_orcid,
            best_orcid,
            person_affiliations_json,
            source_db,
            paper_uid,
            venue,
            year,
            title,
            author_position,
            source_url,
        ) = row
        item = ml.setdefault(
            pid,
            {
                "pid": pid,
                "names": set(),
                "aliases": set(),
                "affiliations": set(),
                "venues": set(),
                "years": set(),
                "orcids": set(),
                "papers": {},
            },
        )
        if canonical_name:
            item["names"].add(canonical_name)
        for alias in (local_name, dblp_author_name):
            if alias:
                item["aliases"].add(alias)
        item["affiliations"].update(json_list(person_affiliations_json))
        item["orcids"].update(json_list(person_orcids_json))
        for oc in (publication_orcid, best_orcid):
            if oc:
                item["orcids"].add(oc)
        if venue:
            item["venues"].add(venue)
        if year is not None:
            item["years"].add(int(year))
            if venue:
                ml_coverage[venue].add(int(year))
        paper_key = (source_db, paper_uid)
        item["papers"][paper_key] = {
            "title": clean(title),
            "venue": clean(venue),
            "year": int(year) if year is not None else None,
            "url": clean(source_url),
            "authorPosition": (
                int(author_position) if author_position is not None else None
            ),
            "localAuthorName": clean(local_name),
        }

    for (
        pid,
        canonical,
        aliases_json,
        affiliations_json,
        orcids_json,
    ) in ml_conn.execute(
        "SELECT dblp_pid, canonical_name, aliases_json, affiliations_json, person_orcids_json FROM dblp_people"
    ):
        if pid not in ml:
            continue
        item = ml[pid]
        if canonical:
            item["names"].add(canonical)
        item["aliases"].update(json_list(aliases_json))
        item["affiliations"].update(json_list(affiliations_json))
        item["orcids"].update(json_list(orcids_json))

    overlap = sorted(set(db) & set(ml))
    people: list[dict[str, Any]] = []
    all_confirmed_papers: set[tuple[str, str]] = set()

    for pid in overlap:
        d, m = db[pid], ml[pid]
        paper_values = sorted(
            m["papers"].values(),
            key=lambda p: (-(p["year"] or 0), p["venue"], p["title"]),
        )
        all_confirmed_papers.update(m["papers"].keys())

        event_values = []
        for (venue, year, role), ev in sorted(
            d["events"].items(),
            key=lambda x: (-(x[0][1] or 0), x[0][0], x[0][2]),
        ):
            event_values.append(
                {
                    "venue": venue,
                    "year": year,
                    "role": role,
                    "rounds": unique(ev["rounds"]),
                    "cycles": unique(ev["cycles"]),
                    "affiliation": choose_name(ev["affiliations"], ""),
                    "sourceUrl": (
                        sorted(ev["sources"])[0] if ev["sources"] else ""
                    ),
                    "observationCount": ev["observations"],
                }
            )

        names = set(d["names"]) | set(m["names"])
        name = choose_name(names, pid)
        db_aliases = unique(d["aliases"] | d["names"])
        ml_aliases = unique(m["aliases"] | m["names"])
        orcids = unique(d["orcids"] | m["orcids"])
        db_years = sorted(d["years"])
        ml_years = sorted(m["years"])

        people.append(
            {
                "id": f"dblp:{pid}",
                "confidence": "confirmed",
                "name": name,
                "dblpPid": pid,
                "orcids": orcids,
                "aliases": {"db": db_aliases, "ml": ml_aliases},
                "affiliations": unique(d["affiliations"] | m["affiliations"]),
                "db": {
                    "venues": unique(d["venues"]),
                    "years": db_years,
                    "roles": unique(d["roles"]),
                    "serviceEventCount": len(event_values),
                    "observationCount": sum(
                        e["observationCount"] for e in event_values
                    ),
                    "firstYear": min(db_years) if db_years else None,
                    "lastYear": max(db_years) if db_years else None,
                    "events": event_values,
                },
                "ml": {
                    "venues": unique(m["venues"]),
                    "years": ml_years,
                    "paperCount": len(paper_values),
                    "firstYear": min(ml_years) if ml_years else None,
                    "lastYear": max(ml_years) if ml_years else None,
                    "papers": paper_values,
                },
            }
        )

    db_name_groups = db_conn.execute(
        "SELECT COUNT(*) FROM name_resolution"
    ).fetchone()[0]
    db_resolved_groups = db_conn.execute(
        "SELECT COUNT(*) FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL"
    ).fetchone()[0]
    db_resolved_pids = db_conn.execute(
        "SELECT COUNT(DISTINCT resolved_dblp_pid) FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL"
    ).fetchone()[0]
    ml_occ = ml_conn.execute(
        "SELECT COUNT(*) FROM author_occurrences"
    ).fetchone()[0]
    ml_resolved_occ = ml_conn.execute(
        "SELECT COUNT(*) FROM author_occurrences WHERE dblp_pid IS NOT NULL"
    ).fetchone()[0]
    ml_resolved_pids = ml_conn.execute(
        "SELECT COUNT(DISTINCT dblp_pid) FROM author_occurrences WHERE dblp_pid IS NOT NULL"
    ).fetchone()[0]

    stats = {
        "dbNameGroups": db_name_groups,
        "dbResolvedNameGroups": db_resolved_groups,
        "dbResolvedPeople": db_resolved_pids,
        "mlAuthorOccurrences": ml_occ,
        "mlResolvedAuthorOccurrences": ml_resolved_occ,
        "mlResolvedPeople": ml_resolved_pids,
        "confirmedMlPapers": len(all_confirmed_papers),
        "confirmedOrcidPeople": sum(1 for p in people if p["orcids"]),
        "coverage": {"db": coverage(db_coverage), "ml": coverage(ml_coverage)},
    }
    return people, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Directory containing enrichment SQLite files (default: {DEFAULT_DATA_DIR})",
    )
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Website data output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = ap.parse_args()

    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = data_dir / DB_ER
    ml_path = data_dir / ML_ER
    for path in (db_path, ml_path):
        if not path.exists():
            raise SystemExit(f"Missing required input: {path}")

    db_conn = sqlite3.connect(db_path)
    ml_conn = sqlite3.connect(ml_path)
    try:
        people, stats = load_confirmed(db_conn, ml_conn)
    finally:
        db_conn.close()
        ml_conn.close()

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = {
        "meta": {
            "dataReady": True,
            "generatedAt": generated,
            "schemaVersion": SCHEMA_VERSION,
            "coverage": stats.pop("coverage"),
            "notes": [
                "Confirmed people require the same persistent DBLP PID on the database-reviewer and ML-publication sides.",
                "Coverage windows differ by venue; counts are not full-career publication histories.",
            ],
        },
        "summary": {
            **stats,
            "confirmedPeople": len(people),
        },
        "people": sorted(
            people,
            key=lambda p: (-p["ml"]["paperCount"], p["name"].casefold()),
        ),
    }

    json_path = output_dir / "site_data.json"
    js_path = output_dir / "site_data.js"
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(text + "\n", encoding="utf-8")
    js_path.write_text(
        "window.CROSSOVER_DATA = " + text + ";\n", encoding="utf-8"
    )

    print(f"Confirmed people: {len(people)}")
    print(f"Confirmed ML papers: {payload['summary']['confirmedMlPapers']}")
    print(f"Wrote: {json_path}")
    print(f"Wrote: {js_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
