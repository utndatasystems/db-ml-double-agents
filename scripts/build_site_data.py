#!/usr/bin/env python3
"""Build the static website dataset from resolved DB service + DBLP publications.

Required inputs in --data-dir:
    entity_resolution.sqlite
    dblp_publications_for_db_people.csv

The publication CSV is produced by ``scan_dblp_publications.py`` and is the
single publication source for the website. Papers are classified as:

- ``main``: exact NeurIPS/NIPS, ICML, or ICLR main-conference publications.
- ``side``: every other paper-like DBLP publication by that resolved person.

Side papers are contextual bibliography. They never establish the core DB↔ML
crossover relation and never contribute to the DB×ML matrix. For the website,
those records are additionally assigned a conservative publication class from
metadata already present in the DBLP snapshot export.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from .display_cleaning import (
        clean_affiliation_label,
        clean_affiliations_with_audit,
        fuzzy_backend,
    )
except ImportError:
    from display_cleaning import (
        clean_affiliation_label,
        clean_affiliations_with_audit,
        fuzzy_backend,
    )

DB_ER = "entity_resolution.sqlite"
PUBLICATIONS_CSV = "dblp_publications_for_db_people.csv"
SCHEMA_VERSION = 6
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "website" / "data"


# Explicit DBLP namespaces only. This is intentionally a compact, conservative
# list: a publication is shown as "Database" only when its DBLP key identifies
# a venue we deliberately recognize as part of the database community.
DATABASE_VENUE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("conf/sigmod/", "SIGMOD"),
    ("conf/vldb/", "VLDB"),
    ("journals/pvldb/", "PVLDB"),
    ("conf/icde/", "ICDE"),
    ("conf/edbt/", "EDBT"),
    ("conf/cidr/", "CIDR"),
    ("conf/pods/", "PODS"),
    ("journals/tods/", "ACM TODS"),
    ("journals/vldb/", "VLDB Journal"),
    ("conf/ssdbm/", "SSDBM"),
    ("conf/dexa/", "DEXA"),
    ("conf/dawak/", "DaWaK"),
)

PUBLICATION_CLASS_LABELS = {
    "main_ml": "Main ML",
    "database": "Database",
    "other_conference": "Other conference",
    "journal": "Journal",
    "workshop": "Workshop",
    "preprint": "Preprint",
    "other": "Other",
}


def clean(value: Any) -> str:
    return "" if value is None else re.sub(r"\s+", " ", str(value).strip())


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [clean(item) for item in value if clean(item)]
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [clean(item) for item in parsed if clean(item)]


def unique(values: Iterable[Any]) -> list[str]:
    return sorted({clean(value) for value in values if clean(value)}, key=str.casefold)


def require_tables(conn: sqlite3.Connection, names: Iterable[str], label: str) -> None:
    have = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = [name for name in names if name not in have]
    if missing:
        raise RuntimeError(f"{label} is missing table(s): {', '.join(missing)}")


def coverage(values: dict[str, set[int]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for venue, years in sorted(values.items()):
        if years:
            result[venue] = {"min": min(years), "max": max(years)}
    return result


def choose_name(names: Iterable[str], fallback: str) -> str:
    values = unique(names)
    if not values:
        return fallback
    return sorted(
        values,
        key=lambda value: (len(normalize_name(value).split()), len(value)),
        reverse=True,
    )[0]


def normalized_metadata(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def known_database_venue(dblp_key: str) -> str:
    key = clean(dblp_key).casefold()
    for prefix, label in DATABASE_VENUE_PREFIXES:
        if key.startswith(prefix):
            return label
    return ""


def classify_publication(
    *,
    scope: str,
    dblp_key: str,
    record_type: str,
    booktitle: str,
    journal: str,
    venue_label: str,
    main_venue: str,
) -> tuple[str, str]:
    """Return (publication_class, exact_venue_label).

    The ordering is deliberate:
    - exact main ML remains the strict crossover signal;
    - CoRR is a preprint even though DBLP stores it as an article;
    - explicit workshop wording wins over a broad DB/conference bucket;
    - known DBLP namespaces identify database venues conservatively;
    - remaining articles and proceedings use structural DBLP record types.
    """

    if scope == "main":
        return "main_ml", clean(main_venue) or clean(venue_label) or "Main ML"

    key = clean(dblp_key)
    key_cf = key.casefold()
    venue_label = clean(venue_label)
    booktitle = clean(booktitle)
    journal = clean(journal)
    record_type = clean(record_type).casefold()

    metadata = " ".join(
        normalized_metadata(value)
        for value in (venue_label, booktitle, journal)
        if clean(value)
    )

    if (
        key_cf.startswith("journals/corr/")
        or normalized_metadata(journal) == "corr"
        or normalized_metadata(venue_label) == "corr"
    ):
        return "preprint", "CoRR"

    # Workshop is only asserted when the DBLP metadata says so explicitly.
    if re.search(r"\bworkshops?\b", metadata):
        return "workshop", booktitle or venue_label or journal or "Workshop"

    db_venue = known_database_venue(key)
    if db_venue:
        return "database", db_venue

    if record_type == "article" or journal:
        return "journal", journal or venue_label or "Journal"

    if record_type == "inproceedings":
        return "other_conference", booktitle or venue_label or "Conference"

    return "other", venue_label or booktitle or journal or "Other DBLP publication"


def load_db_people(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[int]]]:
    require_tables(
        conn,
        ["name_resolution", "reviewer_observations", "dblp_people"],
        "DB enrichment DB",
    )
    people: dict[str, dict[str, Any]] = {}
    venue_coverage: dict[str, set[int]] = defaultdict(set)

    query = """
        SELECT r.resolved_dblp_pid, r.resolved_dblp_name, r.best_orcid,
               r.representative_name, o.name, o.venue, o.year, o.role,
               o.review_round, o.review_cycle, o.affiliation, o.source_url
        FROM name_resolution r
        JOIN reviewer_observations o ON o.name_key=r.name_key
        WHERE r.resolved_dblp_pid IS NOT NULL
        ORDER BY r.resolved_dblp_pid, o.year, o.venue
    """
    for row in conn.execute(query):
        (
            pid,
            resolved_name,
            best_orcid,
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
        pid = clean(pid)
        if not pid:
            continue
        item = people.setdefault(
            pid,
            {
                "pid": pid,
                "names": set(),
                "aliases": set(),
                "affiliations": set(),
                "current_affiliations": set(),
                "former_affiliations": set(),
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
        item["names"].update(
            clean(value) for value in (resolved_name, representative) if clean(value)
        )
        if clean(observed_name):
            item["aliases"].add(clean(observed_name))
        if clean(affiliation):
            item["affiliations"].add(clean(affiliation))
        if clean(venue):
            item["venues"].add(clean(venue))
        if clean(role):
            item["roles"].add(clean(role))
        if year is not None:
            item["years"].add(int(year))
            if clean(venue):
                venue_coverage[clean(venue)].add(int(year))
        if clean(best_orcid):
            item["orcids"].add(clean(best_orcid))

        event_key = (clean(venue), int(year) if year is not None else None, clean(role))
        event = item["events"][event_key]
        event["observations"] += 1
        if clean(review_round):
            event["rounds"].add(clean(review_round))
        if clean(review_cycle):
            event["cycles"].add(clean(review_cycle))
        if clean(affiliation):
            event["affiliations"].add(clean(affiliation))
        if clean(source_url):
            event["sources"].add(clean(source_url))

    dblp_people_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(dblp_people)")
    }
    have_affiliation_status = {
        "current_affiliations_json", "former_affiliations_json"
    }.issubset(dblp_people_columns)
    affiliation_select = (
        "current_affiliations_json, former_affiliations_json"
        if have_affiliation_status
        else "'[]' AS current_affiliations_json, '[]' AS former_affiliations_json"
    )
    query = f"""
        SELECT dblp_pid, canonical_name, aliases_json, affiliations_json,
               {affiliation_select}, verified_orcids_json
        FROM dblp_people
    """
    for (
        pid, canonical, aliases_json, affiliations_json,
        current_affiliations_json, former_affiliations_json, orcids_json
    ) in conn.execute(query):
        pid = clean(pid)
        if pid not in people:
            continue
        item = people[pid]
        if clean(canonical):
            item["names"].add(clean(canonical))
        item["aliases"].update(json_list(aliases_json))
        item["affiliations"].update(json_list(affiliations_json))
        item["current_affiliations"].update(json_list(current_affiliations_json))
        item["former_affiliations"].update(json_list(former_affiliations_json))
        item["orcids"].update(json_list(orcids_json))

    return people, venue_coverage


def load_publications(
    path: Path,
    known_pids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, set[int]]]:
    if not path.exists():
        raise RuntimeError(
            f"Missing {path.name}. Run scripts/scan_dblp_publications.py first."
        )

    by_pid: dict[str, dict[str, Any]] = {}
    main_coverage: dict[str, set[int]] = defaultdict(set)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = clean(row.get("dblp_pid"))
            if not pid or pid not in known_pids:
                continue
            try:
                year = int(row.get("year") or "")
            except ValueError:
                continue
            scope = clean(row.get("scope")).casefold()
            scope = "main" if scope == "main" else "side"
            title = clean(row.get("title"))
            key = clean(row.get("dblp_key"))
            if not title or not key:
                continue

            item = by_pid.setdefault(
                pid,
                {
                    "names": set(),
                    "aliases": set(),
                    "papers": {},
                },
            )
            if clean(row.get("canonical_name")):
                item["names"].add(clean(row.get("canonical_name")))
            if clean(row.get("author_signature")):
                item["aliases"].add(clean(row.get("author_signature")))

            venue = clean(row.get("venue")) if scope == "main" else ""
            venue_label = clean(row.get("venue_label")) or venue or "Other DBLP publication"
            record_type = clean(row.get("record_type"))
            booktitle = clean(row.get("booktitle"))
            journal = clean(row.get("journal"))
            publication_class, exact_venue = classify_publication(
                scope=scope,
                dblp_key=key,
                record_type=record_type,
                booktitle=booktitle,
                journal=journal,
                venue_label=venue_label,
                main_venue=venue,
            )
            try:
                author_position = int(row.get("author_position") or "") - 1
            except ValueError:
                author_position = None

            paper = {
                "title": title,
                "scope": scope,
                "venue": venue,
                "venueLabel": venue_label,
                "publicationClass": publication_class,
                "publicationClassLabel": PUBLICATION_CLASS_LABELS[publication_class],
                "exactVenue": exact_venue,
                "year": year,
                "url": clean(row.get("url")),
                "dblpKey": key,
                "authorPosition": author_position,
                "localAuthorName": clean(row.get("author_signature")),
                "recordType": record_type,
                "publtype": clean(row.get("publtype")),
                "booktitle": booktitle,
                "journal": journal,
                "source": "dblp_snapshot",
                "matchBasis": clean(row.get("match_basis")),
            }
            item["papers"][key] = paper
            if scope == "main" and venue:
                main_coverage[venue].add(year)

    return by_pid, main_coverage


def build_payload(
    db_conn: sqlite3.Connection,
    publications_csv: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    db, db_coverage = load_db_people(db_conn)
    publications, main_coverage = load_publications(publications_csv, set(db))

    people: list[dict[str, Any]] = []
    cleanup_audit: list[dict[str, Any]] = []
    unique_main_keys: set[str] = set()
    unique_side_keys: set[str] = set()
    unique_class_keys: dict[str, set[str]] = defaultdict(set)
    total_service_events = 0

    for pid in sorted(db):
        d = db[pid]
        p = publications.get(pid, {"names": set(), "aliases": set(), "papers": {}})
        names = set(d["names"]) | set(p["names"])
        name = choose_name(names, pid)

        raw_affiliations = unique(d["affiliations"])
        cleaned_affiliations, person_cleanup = clean_affiliations_with_audit(raw_affiliations)
        person_display_map = {
            raw: clean_affiliation_label(raw) for raw in raw_affiliations
        }
        person_reason_map: dict[str, str] = {}
        for change in person_cleanup:
            person_display_map[change["raw"]] = change["display"]
            person_reason_map[change["raw"]] = change["reason"]
            cleanup_audit.append(
                {
                    "scope": "person",
                    "dblpPid": pid,
                    "personName": name,
                    "venue": "",
                    "year": "",
                    "role": "",
                    **change,
                }
            )

        raw_current_affiliations = unique(d["current_affiliations"])
        display_current_affiliations = unique(
            person_display_map.get(raw, clean_affiliation_label(raw))
            for raw in raw_current_affiliations
        )
        raw_former_affiliations = unique(d["former_affiliations"])
        display_former_affiliations = unique(
            person_display_map.get(raw, clean_affiliation_label(raw))
            for raw in raw_former_affiliations
        )

        event_values: list[dict[str, Any]] = []
        for (venue, year, role), event in sorted(
            d["events"].items(), key=lambda item: (-(item[0][1] or 0), item[0][0], item[0][2])
        ):
            raw_event_affiliations = unique(event["affiliations"])
            local_display, local_cleanup = clean_affiliations_with_audit(raw_event_affiliations)
            event_display_map = {
                raw: clean_affiliation_label(raw) for raw in raw_event_affiliations
            }
            event_reason_map: dict[str, str] = {}
            for change in local_cleanup:
                event_display_map[change["raw"]] = change["display"]
                event_reason_map[change["raw"]] = change["reason"]

            display_event_affiliations = unique(
                person_display_map.get(raw, event_display_map.get(raw, clean_affiliation_label(raw)))
                for raw in raw_event_affiliations
            )
            for raw in raw_event_affiliations:
                display = person_display_map.get(
                    raw, event_display_map.get(raw, clean_affiliation_label(raw))
                )
                if raw == display:
                    continue
                reasons: list[str] = []
                if event_reason_map.get(raw):
                    reasons.append(event_reason_map[raw])
                if person_reason_map.get(raw) and person_reason_map[raw] not in reasons:
                    reasons.append(person_reason_map[raw])
                if person_reason_map.get(raw) and display != event_display_map.get(raw):
                    reasons.append("person-canonical")
                cleanup_audit.append(
                    {
                        "scope": "service-event",
                        "dblpPid": pid,
                        "personName": name,
                        "venue": venue or "",
                        "year": year if year is not None else "",
                        "role": role or "",
                        "raw": raw,
                        "cleaned": clean_affiliation_label(raw),
                        "display": display,
                        "reason": "+".join(dict.fromkeys(reasons)) or "person-canonical",
                    }
                )

            event_values.append(
                {
                    "venue": venue,
                    "year": year,
                    "role": role,
                    "rounds": unique(event["rounds"]),
                    "cycles": unique(event["cycles"]),
                    "affiliation": unique(event["affiliations"])[0] if event["affiliations"] else "",
                    "rawAffiliations": raw_event_affiliations,
                    "displayAffiliations": display_event_affiliations or local_display,
                    "displayAffiliation": (
                        (display_event_affiliations or local_display)[0]
                        if (display_event_affiliations or local_display)
                        else ""
                    ),
                    "sourceUrl": sorted(event["sources"])[0] if event["sources"] else "",
                    "observationCount": event["observations"],
                }
            )

        papers = sorted(
            p["papers"].values(),
            key=lambda paper: (
                -(paper.get("year") or 0),
                0 if paper.get("scope") == "main" else 1,
                clean(paper.get("venue") or paper.get("venueLabel")),
                clean(paper.get("title")),
            ),
        )
        main_papers = [paper for paper in papers if paper["scope"] == "main"]
        side_papers = [paper for paper in papers if paper["scope"] == "side"]
        unique_main_keys.update(paper["dblpKey"] for paper in main_papers)
        unique_side_keys.update(paper["dblpKey"] for paper in side_papers)
        for paper in papers:
            unique_class_keys[paper["publicationClass"]].add(paper["dblpKey"])
        total_service_events += len(event_values)

        main_years = sorted({paper["year"] for paper in main_papers})
        side_years = sorted({paper["year"] for paper in side_papers})
        all_years = sorted(set(main_years) | set(side_years))
        main_venues = unique(paper["venue"] for paper in main_papers)
        publication_classes = unique(paper["publicationClass"] for paper in papers)
        class_counts = {
            class_name: sum(1 for paper in papers if paper["publicationClass"] == class_name)
            for class_name in PUBLICATION_CLASS_LABELS
        }

        latest_service_affiliation = next(
            (event.get("displayAffiliation") for event in event_values if event.get("displayAffiliation")),
            "",
        )
        primary_affiliation = (
            display_current_affiliations[0]
            if display_current_affiliations
            else latest_service_affiliation
            or (cleaned_affiliations[0] if cleaned_affiliations else "")
        )

        people.append(
            {
                "id": f"dblp:{pid}",
                "confidence": "confirmed",
                "name": name,
                "dblpPid": pid,
                "orcids": unique(d["orcids"]),
                "aliases": {
                    "db": unique(d["aliases"] | d["names"]),
                    "publication": unique(p["aliases"]),
                },
                "affiliations": raw_affiliations,
                "displayAffiliations": cleaned_affiliations,
                "currentAffiliations": raw_current_affiliations,
                "displayCurrentAffiliations": display_current_affiliations,
                "formerAffiliations": raw_former_affiliations,
                "displayFormerAffiliations": display_former_affiliations,
                "primaryAffiliation": primary_affiliation,
                "db": {
                    "venues": unique(d["venues"]),
                    "years": sorted(d["years"]),
                    "roles": unique(d["roles"]),
                    "serviceEventCount": len(event_values),
                    "observationCount": sum(event["observationCount"] for event in event_values),
                    "firstYear": min(d["years"]) if d["years"] else None,
                    "lastYear": max(d["years"]) if d["years"] else None,
                    "events": event_values,
                },
                "ml": {
                    "venues": main_venues,
                    "years": main_years,
                    "mainYears": main_years,
                    "sideYears": side_years,
                    "allPublicationYears": all_years,
                    "paperCount": len(papers),
                    "mainPaperCount": len(main_papers),
                    "sidePaperCount": len(side_papers),
                    "hasMain": bool(main_papers),
                    "hasSide": bool(side_papers),
                    "publicationClasses": publication_classes,
                    "publicationClassCounts": class_counts,
                    "firstYear": min(all_years) if all_years else None,
                    "lastYear": max(all_years) if all_years else None,
                    "papers": papers,
                },
            }
        )

    db_name_groups = db_conn.execute("SELECT COUNT(*) FROM name_resolution").fetchone()[0]
    db_resolved_groups = db_conn.execute(
        "SELECT COUNT(*) FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL"
    ).fetchone()[0]
    db_resolved_pids = db_conn.execute(
        "SELECT COUNT(DISTINCT resolved_dblp_pid) FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL"
    ).fetchone()[0]

    changed_people = {row["dblpPid"] for row in cleanup_audit if row["scope"] == "person"}
    changed_events = {
        (row["dblpPid"], row["venue"], row["year"], row["role"])
        for row in cleanup_audit
        if row["scope"] == "service-event"
    }

    stats = {
        "dbNameGroups": db_name_groups,
        "dbResolvedNameGroups": db_resolved_groups,
        "dbResolvedPeople": db_resolved_pids,
        "sitePeople": len(people),
        "mainCrossoverPeople": sum(1 for person in people if person["ml"]["hasMain"]),
        "sideOnlyPeople": sum(
            1 for person in people if person["ml"]["hasSide"] and not person["ml"]["hasMain"]
        ),
        "uniqueMainPapers": len(unique_main_keys),
        "uniqueSidePapers": len(unique_side_keys),
        "publicationClassCounts": {
            class_name: len(unique_class_keys[class_name])
            for class_name in PUBLICATION_CLASS_LABELS
        },
        "dbServiceEvents": total_service_events,
        "affiliationCleanupChangedPeople": len(changed_people),
        "affiliationCleanupChangedEvents": len(changed_events),
        "affiliationCleanupChangeRows": len(cleanup_audit),
        "coverage": {
            "db": coverage(db_coverage),
            "mlMain": coverage(main_coverage),
        },
    }
    return people, stats, cleanup_audit


def write_cleanup_audit(data_dir: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    reports = data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    csv_path = reports / "affiliation_cleanup_report.csv"
    json_path = reports / "affiliation_cleanup_report.json"
    fields = [
        "scope",
        "dblpPid",
        "personName",
        "venue",
        "year",
        "role",
        "raw",
        "cleaned",
        "display",
        "reason",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    db_path = data_dir / DB_ER
    publications_csv = data_dir / PUBLICATIONS_CSV

    if not db_path.exists():
        raise SystemExit(f"Missing required input: {db_path}")
    if not publications_csv.exists():
        raise SystemExit(
            f"Missing required input: {publications_csv}\n"
            "Run .\\run_dblp_publication_scan.bat first."
        )

    conn = sqlite3.connect(db_path)
    try:
        people, summary, cleanup_audit = build_payload(conn, publications_csv)
    finally:
        conn.close()

    payload = {
        "meta": {
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "schemaVersion": SCHEMA_VERSION,
            "dataReady": True,
            "identityKey": "DBLP PID",
            "publicationSource": "DBLP snapshot",
            "mainVenueDefinition": "exact NeurIPS/NIPS, ICML, or ICLR main-conference DBLP record",
            "sidePaperDefinition": "other paper-like DBLP publication; context only, not ML crossover evidence",
            "publicationClasses": PUBLICATION_CLASS_LABELS,
            "publicationClassDefinition": "conservative classification from DBLP key, record type, booktitle, journal, and venue label; workshop requires explicit DBLP workshop wording",
            "defaultResearcherScope": "core",
            "affiliationDisplayCleaning": True,
            "affiliationCurrentFormerAware": True,
            "affiliationFuzzyBackend": fuzzy_backend(),
        },
        "summary": summary,
        "people": people,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "site_data.json"
    js_path = output_dir / "site_data.js"
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(json_text, encoding="utf-8")
    js_path.write_text(f"window.CROSSOVER_DATA = {json_text};\n", encoding="utf-8")

    cleanup_csv, cleanup_json = write_cleanup_audit(data_dir, cleanup_audit)

    print(f"Resolved DB people in site dataset: {summary['sitePeople']}")
    print(f"Core main-venue crossover people:  {summary['mainCrossoverPeople']}")
    print(f"Unique main ML papers:             {summary['uniqueMainPapers']}")
    print(f"Unique broader DBLP papers:        {summary['uniqueSidePapers']}")
    print("Publication classes (unique papers):")
    for class_name, label in PUBLICATION_CLASS_LABELS.items():
        print(f"  {label:<18} {summary['publicationClassCounts'][class_name]:>7}")
    print(
        "Affiliation cleanup: "
        f"{summary['affiliationCleanupChangedPeople']} people, "
        f"{summary['affiliationCleanupChangedEvents']} service events, "
        f"{summary['affiliationCleanupChangeRows']} changed raw labels "
        f"({payload['meta']['affiliationFuzzyBackend']})"
    )
    print(f"Wrote: {json_path}")
    print(f"Wrote: {js_path}")
    print(f"Cleanup audit CSV: {cleanup_csv}")
    print(f"Cleanup audit JSON: {cleanup_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
