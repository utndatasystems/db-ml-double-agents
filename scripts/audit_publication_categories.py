"""Audit every contextual work against the pinned DBLP metadata and mapping CSV.

Writes per-work and per-venue CSVs plus a JSON summary under data/reports.
This tool never edits the category mappings or changes publication scope.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

try:
    from .build_site_data import classify_publication
    from .enrich_reviewers_with_dblp import SNAPSHOT_FILENAME
    from .publication_categories import MAPPINGS_PATH, load_category_mappings, matching_category_rule
    from .publication_category_reviews import REVIEWS_PATH, load_category_reviews, annotate_category_reviews
except ImportError:
    from build_site_data import classify_publication
    from enrich_reviewers_with_dblp import SNAPSHOT_FILENAME
    from publication_categories import MAPPINGS_PATH, load_category_mappings, matching_category_rule
    from publication_category_reviews import REVIEWS_PATH, load_category_reviews, annotate_category_reviews

RECORD_START = re.compile(br'<(article|inproceedings|incollection|proceedings|book)\b[^>]*\bkey="([^"]+)"')
WORKSHOP = re.compile(r"\bworkshops?\b", re.I)


def read_snapshot_evidence(path: Path, keys: set[str], *, chunk_size: int = 8 * 1024 * 1024) -> dict:
    """Read selected records and parent volumes, including adjacent XML records.

    Only bibliographic strings are extracted; this is an audit, not ingestion.
    Chunk boundaries may occur anywhere, including inside a record header.
    """
    records, parents = {}, {}
    pending = b""
    with gzip.open(path, "rb") as handle:
        while chunk := handle.read(chunk_size):
            buffer = pending + chunk
            position = 0
            while True:
                match = RECORD_START.search(buffer, position)
                if not match:
                    last = buffer.rfind(b"<", position)
                    pending = buffer[last:] if last >= 0 else buffer[-1024:]
                    break
                tag, key = match.group(1), match.group(2).decode("utf-8")
                if key not in keys and tag not in (b"proceedings", b"book"):
                    position = match.end()
                    continue
                closing = b"</" + tag + b">"
                end = buffer.find(closing, match.end())
                if end < 0:
                    pending = buffer[match.start():]
                    break
                block = buffer[match.start():end + len(closing)].decode("utf-8")
                position = end + len(closing)
                record = {"record_type": tag.decode("ascii")}
                for field in ("title", "booktitle", "journal", "crossref", "year"):
                    value = re.search(r"<" + field + r"(?:\s[^>]*)?>(.*?)</" + field + ">", block, re.S)
                    record[field] = html.unescape(re.sub("<[^>]+>", "", value.group(1))).strip() if value else ""
                if key in keys:
                    records[key] = record
                if tag in (b"proceedings", b"book"):
                    parents[key] = record
    return {"snapshot": path.name.removesuffix(".xml.gz"), "records": records,
            "parents": parents, "missing_keys": sorted(keys - records.keys())}


def audit_rows(rows: list[dict], evidence: dict) -> list[dict]:
    result = []
    for row in rows:
        key = row["dblp_key"]
        metadata = evidence["records"].get(key, {})
        parent_key = metadata.get("crossref", "")
        parent_title = evidence["parents"].get(parent_key, {}).get("title", "")
        category, label = classify_publication(
            scope="side", dblp_key=key, year=int(row["year"]), record_type=row["record_type"],
            booktitle=row["booktitle"], journal=row["journal"], venue_label=row["venue_label"], main_venue="",
        )
        rule = matching_category_rule(dblp_key=key, booktitle=row["booktitle"],
                                      journal=row["journal"], venue_label=row["venue_label"], year=row["year"])
        mapped = rule is not None and rule.category == category and category != "preprint"
        if not metadata:
            status = "missing_snapshot_record"
        elif category == "preprint":
            status = "preprint_metadata"
        elif mapped:
            status = "explicit_mapping"
        elif category == "workshop":
            status = "explicit_workshop_metadata"
        elif WORKSHOP.search(parent_title):
            status = "review_mixed_or_ambiguous_volume"
        elif category == "journal":
            status = "journal_record_metadata"
        elif category == "other_conference":
            status = "proceedings_type_fallback"
        else:
            status = "other_record_metadata"
        result.append({
            "dblp_key": key, "work_id": row.get("work_id") or key,
            "title": row.get("title", ""),
            "year": row["year"], "namespace": "/".join(key.split("/")[:2]),
            "booktitle": row["booktitle"], "journal": row["journal"],
            "record_type": row["record_type"], "category": category, "venue_label": label,
            "status": status, "rule_id": rule.rule_id if mapped else "",
            "evidence_url": rule.evidence_url if mapped else ("https://dblp.org/rec/" + parent_key if parent_key else "https://dblp.org/rec/" + key),
            "reason": rule.reason if mapped else status.replace("_", " "),
            "parent_key": parent_key, "parent_title": parent_title,
        })
    return result


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--snapshot", type=Path, help="Pinned .xml.gz; defaults to the project's pinned snapshot")
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    report_dir = data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    unique = {}
    associations = 0
    with (data_dir / "dblp_publication_entities_for_db_people.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["scope"] == "side":
                unique.setdefault(row["dblp_key"], row)
                associations += 1
    snapshot = args.snapshot or data_dir / "dblp_snapshot" / SNAPSHOT_FILENAME
    cache = report_dir / "publication_category_snapshot_evidence.json"
    evidence = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
    if (evidence is None or evidence.get("snapshot") != snapshot.name.removesuffix(".xml.gz")
            or snapshot.stat().st_mtime > cache.stat().st_mtime
            or not set(unique).issubset(evidence["records"])):
        evidence = read_snapshot_evidence(snapshot, set(unique))
        cache.write_text(json.dumps(evidence, ensure_ascii=True), encoding="utf-8")
    if any(key not in evidence["records"] for key in unique):
        raise RuntimeError("Some contextual records are absent from the pinned snapshot; inspect the evidence cache")
    rows = audit_rows(list(unique.values()), evidence)
    categories_by_work = defaultdict(set)
    for row in rows:
        categories_by_work[row["work_id"]].add(row["category"])
    version_specific = {key for key, categories in categories_by_work.items() if len(categories) > 1}
    for row in rows:
        if row["work_id"] in version_specific:
            row["status"] = "review_version_specific_category"
            row["reason"] += "; different researcher-visible versions of this work have different categories"
    reviews = load_category_reviews()
    annotate_category_reviews(rows, reviews)
    write_csv(report_dir / "publication_category_audit.csv", rows)
    review = [row for row in rows if row["status"].startswith("review_")]
    write_csv(report_dir / "publication_category_review.csv", review)
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[key] for key in ("namespace", "booktitle", "journal", "category", "status"))].append(row)
    venues = []
    for group, items in sorted(groups.items()):
        venues.append(dict(zip(("namespace", "booktitle", "journal", "category", "status"), group),
                           unique_works=len({item["work_id"] for item in items}), rule_ids=";".join(sorted({item["rule_id"] for item in items if item["rule_id"]})),
                           example_dblp_key=items[0]["dblp_key"], evidence_url=items[0]["evidence_url"]))
    write_csv(report_dir / "publication_category_venues.csv", venues)
    rules = load_category_mappings()
    used = {row["rule_id"] for row in rows}
    summary = {"snapshot": evidence["snapshot"], "unique_contextual_records": len(rows),
               "unique_contextual_works": len(categories_by_work),
               "researcher_work_associations": associations, "venue_groups": len(venues),
               "categories_by_record": dict(Counter(row["category"] for row in rows)),
               "category_memberships_by_work": dict(Counter(category for categories in categories_by_work.values() for category in categories)),
               "statuses": dict(Counter(row["status"] for row in rows)),
               "mapping_file": "data/" + MAPPINGS_PATH.name, "mapping_rows": len(rules),
               "review_file": "data/" + REVIEWS_PATH.name,
               "reviewed_records": sum(row["status"] == "reviewed_assignment" for row in rows),
               "pending_review_records": len(review),
               "changed_review_records": sum(row["status"] in {"review_metadata_changed", "review_assignment_conflict"} for row in rows),
               "reviewed_keys_not_in_corpus": sorted(set(reviews) - {row["dblp_key"] for row in rows}),
               "unused_mapping_rules": [rule.rule_id for rule in rules if rule.rule_id not in used],
               "needs_individual_track_review": sum(row["status"] in {
                   "review_mixed_or_ambiguous_volume", "review_pending",
                   "review_metadata_changed", "review_assignment_conflict"
               } for row in review),
               "version_specific_category_work_ids": sorted(version_specific),
               "limit": "Every work checked against available metadata; structural fallbacks and mixed volumes are not a claim of independently verified venue semantics."}
    (report_dir / "publication_category_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if any(row['manual_review_status'] == 'confirmed' and row['status'] in {
        'review_metadata_changed', 'review_assignment_conflict'
    } for row in rows):
        raise RuntimeError('A confirmed publication review no longer matches its record or category; inspect publication_category_review.csv before rebuilding')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
