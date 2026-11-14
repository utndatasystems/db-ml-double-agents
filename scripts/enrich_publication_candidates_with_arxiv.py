#!/usr/bin/env python3
"""Enrich unresolved DBLP publication-version candidates with arXiv metadata.

This script refreshes cached arXiv metadata used as supporting evidence for
publication entity resolution. By default it selects preprints from the current
review queue. With ``--all-unresolved`` it instead selects every CoRR preprint
whose current publication-entity component contains no archival DBLP publication.
In both modes it extracts exact arXiv identifiers already present in DBLP metadata,
fetches metadata by ``id_list`` from the public arXiv API, and writes the same
enriched review CSV. The script itself does not change merge decisions; the
publication resolver consumes the cache on its next pass.

Inputs
------
- data/dblp_publications_for_db_people.csv
- data/publication_entity_resolution.csv (required by ``--all-unresolved``)
- data/reports/publication_entity_candidates.csv

Outputs
-------
- data/reports/publication_entity_candidates_arxiv.csv
- data/reports/arxiv_candidate_metadata_cache.json

The cache is updated after each successful API batch so interrupted runs can be
resumed without repeating completed requests. Use ``--offline`` to rebuild the
CSV from the local cache only, or ``--refresh`` to re-fetch selected IDs.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from .enrich_reviewers_with_dblp import DEFAULT_DATA_DIR, clean_text
    from .publication_arxiv import (
        arxiv_id_from_publication_values,
        extract_arxiv_id,
        normalize_arxiv_id,
    )
    from .resolve_publication_entities import normalize_doi, normalized_title
except ImportError:  # Running as ``python scripts/enrich_publication_candidates_with_arxiv.py``
    from enrich_reviewers_with_dblp import DEFAULT_DATA_DIR, clean_text
    from publication_arxiv import (
        arxiv_id_from_publication_values,
        extract_arxiv_id,
        normalize_arxiv_id,
    )
    from resolve_publication_entities import normalize_doi, normalized_title

RAW_PUBLICATIONS_CSV = "dblp_publications_for_db_people.csv"
CANDIDATES_CSV = "publication_entity_candidates.csv"
MAPPING_CSV = "publication_entity_resolution.csv"
OUTPUT_CSV = "publication_entity_candidates_arxiv.csv"
CACHE_JSON = "arxiv_candidate_metadata_cache.json"
CACHE_VERSION = 1

ARXIV_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_BATCH_SIZE = 50
DEFAULT_DELAY_SECONDS = 3.0
USER_AGENT = "db-x-ml-publication-er/0.1 (publication metadata research)"

ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
NS = {"atom": ATOM_NS, "arxiv": ARXIV_NS}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not call arXiv; use only metadata already present in the cache.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-fetch selected arXiv IDs even when cached metadata exists.",
    )
    parser.add_argument(
        "--all-unresolved",
        action="store_true",
        help=(
            "Select every CoRR preprint whose current resolved work contains no "
            "archival DBLP publication, rather than only preprints in the review queue."
        ),
    )
    parser.add_argument(
        "--limit-preprints",
        type=int,
        help="Fetch at most this many unique preprints (useful for a smoke test).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"arXiv IDs per request (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=(
            "Seconds between arXiv API requests. The arXiv API manual encourages "
            f"a 3-second delay for repeated calls (default: {DEFAULT_DELAY_SECONDS:g})."
        ),
    )
    return parser.parse_args(argv)


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        raise RuntimeError(f"Missing required input: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise RuntimeError(f"Missing CSV header in {path}")
        return [dict(row) for row in reader], list(reader.fieldnames)


def json_string_list(value: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(item) for item in parsed if clean_text(item)]


def arxiv_id_from_raw_row(row: dict[str, str]) -> str:
    return arxiv_id_from_publication_values(
        url=clean_text(row.get("url")),
        ees=json_string_list(row.get("ees_json", "")),
        dblp_key=clean_text(row.get("dblp_key")),
    )


def unique_raw_records(rows: Iterable[dict[str, str]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        key = clean_text(row.get("dblp_key"))
        if key and key not in result:
            result[key] = row
    return result


def is_preprint_row(row: dict[str, str]) -> bool:
    key = clean_text(row.get("dblp_key")).casefold()
    journal = clean_text(row.get("journal")).casefold()
    return key.startswith("journals/corr/") or journal == "corr"


def is_archival_row(row: dict[str, str]) -> bool:
    return (
        not is_preprint_row(row)
        and clean_text(row.get("record_type")).casefold() in {"article", "inproceedings"}
        and clean_text(row.get("publtype")).casefold() != "informal"
    )


def unresolved_preprint_keys(
    raw_by_key: dict[str, dict[str, str]],
    mapping_rows: Iterable[dict[str, str]],
) -> list[str]:
    """Return CoRR records in work components with no archival DBLP member."""
    work_members: dict[str, set[str]] = {}
    for row in mapping_rows:
        work_id = clean_text(row.get("work_id"))
        key = clean_text(row.get("dblp_key"))
        if work_id and key:
            work_members.setdefault(work_id, set()).add(key)

    selected: list[str] = []
    for work_id in sorted(work_members):
        keys = sorted(work_members[work_id])
        members = [raw_by_key[key] for key in keys if key in raw_by_key]
        if any(is_archival_row(row) for row in members):
            continue
        selected.extend(
            key for key in keys
            if key in raw_by_key and is_preprint_row(raw_by_key[key])
        )
    return sorted(set(selected))


def published_dois(row: dict[str, str]) -> list[str]:
    values = [clean_text(row.get("url")), *json_string_list(row.get("ees_json", ""))]
    return sorted({doi for value in values if (doi := normalize_doi(value))})


def _text(entry: ET.Element, path: str) -> str:
    node = entry.find(path, NS)
    return clean_text(node.text if node is not None else "")


def parse_arxiv_atom(payload: bytes) -> dict[str, dict[str, Any]]:
    """Parse article metadata from an arXiv Atom API response."""
    root = ET.fromstring(payload)
    result: dict[str, dict[str, Any]] = {}
    for entry in root.findall("atom:entry", NS):
        entry_id = _text(entry, "atom:id")
        if "/api/errors#" in entry_id:
            continue
        arxiv_id = normalize_arxiv_id(entry_id)
        if not arxiv_id:
            continue

        authors = [
            clean_text(author.findtext(f"{{{ATOM_NS}}}name") or "")
            for author in entry.findall("atom:author", NS)
        ]
        authors = [author for author in authors if author]
        categories = sorted(
            {
                clean_text(category.attrib.get("term", ""))
                for category in entry.findall("atom:category", NS)
                if clean_text(category.attrib.get("term", ""))
            }
        )
        primary = entry.find("arxiv:primary_category", NS)
        primary_category = clean_text(primary.attrib.get("term", "")) if primary is not None else ""

        doi = normalize_doi(_text(entry, "arxiv:doi"))
        if not doi:
            for link in entry.findall("atom:link", NS):
                if clean_text(link.attrib.get("title", "")).casefold() != "doi":
                    continue
                doi = normalize_doi(clean_text(link.attrib.get("href", "")))
                if doi:
                    break

        result[arxiv_id] = {
            "arxiv_id": arxiv_id,
            "title": _text(entry, "atom:title"),
            "authors": authors,
            "published": _text(entry, "atom:published"),
            "updated": _text(entry, "atom:updated"),
            "summary": _text(entry, "atom:summary"),
            "doi": doi,
            "journal_ref": _text(entry, "arxiv:journal_ref"),
            "comment": _text(entry, "arxiv:comment"),
            "primary_category": primary_category,
            "categories": categories,
            "abs_url": entry_id,
            "status": "found",
        }
    return result


def load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return {}
    records = payload.get("records")
    if not isinstance(records, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for stored_id, record in records.items():
        if not isinstance(record, dict):
            continue
        canonical_id = normalize_arxiv_id(stored_id)
        if not canonical_id:
            continue
        # A historical DBLP spelling such as cs.IR/0407061 can have a cached
        # ``not_found`` result even though arXiv serves the canonical cs/0407061.
        # Drop only those stale negative aliases so the canonical id is fetched.
        if (
            canonical_id != str(stored_id).strip()
            and clean_text(record.get("status")).casefold() != "found"
        ):
            continue
        result[canonical_id] = record
    return result


def save_cache(path: Path, records: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "source": ARXIV_API_URL,
        "refreshedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "records": {key: records[key] for key in sorted(records)},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def fetch_arxiv_batch(ids: Sequence[str], *, timeout: float = 60.0) -> dict[str, dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "id_list": ",".join(ids),
            "max_results": str(len(ids)),
        }
    )
    request = urllib.request.Request(
        f"{ARXIV_API_URL}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"arXiv API request failed: {exc}") from exc
    return parse_arxiv_atom(payload)


def enrich_candidate_rows(
    candidates: list[dict[str, str]],
    raw_by_key: dict[str, dict[str, str]],
    metadata: dict[str, dict[str, Any]],
    *,
    requested_ids: set[str] | None = None,
) -> list[dict[str, str]]:
    requested_ids = requested_ids if requested_ids is not None else set(metadata)
    enriched: list[dict[str, str]] = []
    for candidate in candidates:
        result = dict(candidate)
        preprint_key = clean_text(candidate.get("preprint_key"))
        published_key = clean_text(candidate.get("published_key"))
        preprint = raw_by_key.get(preprint_key, {})
        published = raw_by_key.get(published_key, {})
        arxiv_id = arxiv_id_from_raw_row(preprint)
        record = metadata.get(arxiv_id, {}) if arxiv_id else {}

        if not arxiv_id:
            status = "missing_arxiv_id"
        elif arxiv_id not in requested_ids and arxiv_id not in metadata:
            status = "not_requested"
        elif record:
            status = clean_text(record.get("status")) or "found"
        else:
            status = "not_found_or_not_cached"

        published_doi_values = published_dois(published)
        arxiv_doi = normalize_doi(str(record.get("doi", ""))) if record else ""
        current_title = clean_text(record.get("title")) if record else ""
        title_match = bool(
            current_title
            and clean_text(candidate.get("published_title"))
            and normalized_title(current_title)
            == normalized_title(clean_text(candidate.get("published_title")))
        )
        doi_match = bool(arxiv_doi and arxiv_doi in published_doi_values)

        evidence: list[str] = []
        if doi_match:
            evidence.append("arxiv_doi_matches_published")
        if title_match:
            evidence.append("arxiv_current_title_matches_published")

        result.update(
            {
                "arxiv_id": arxiv_id,
                "arxiv_metadata_status": status,
                "arxiv_title": current_title,
                "arxiv_authors_json": json.dumps(record.get("authors", []), ensure_ascii=False),
                "arxiv_published": clean_text(record.get("published")) if record else "",
                "arxiv_updated": clean_text(record.get("updated")) if record else "",
                "arxiv_doi": arxiv_doi,
                "arxiv_journal_ref": clean_text(record.get("journal_ref")) if record else "",
                "arxiv_comment": clean_text(record.get("comment")) if record else "",
                "arxiv_primary_category": clean_text(record.get("primary_category")) if record else "",
                "arxiv_categories_json": json.dumps(record.get("categories", []), ensure_ascii=False),
                "arxiv_abs_url": clean_text(record.get("abs_url")) if record else "",
                "published_venue_label": clean_text(published.get("venue_label")),
                "published_booktitle": clean_text(published.get("booktitle")),
                "published_journal": clean_text(published.get("journal")),
                "published_dois_json": json.dumps(published_doi_values, ensure_ascii=False),
                "arxiv_doi_matches_published": "1" if doi_match else "0",
                "arxiv_current_title_matches_published": "1" if title_match else "0",
                "arxiv_evidence": ";".join(evidence),
            }
        )
        enriched.append(result)
    return enriched


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data_dir.resolve()
    reports_dir = data_dir / "reports"
    candidates_path = reports_dir / CANDIDATES_CSV
    raw_path = data_dir / RAW_PUBLICATIONS_CSV
    output_path = reports_dir / OUTPUT_CSV
    cache_path = reports_dir / CACHE_JSON

    if args.batch_size <= 0:
        raise RuntimeError("--batch-size must be positive")
    if args.delay < 0:
        raise RuntimeError("--delay must be non-negative")
    if args.limit_preprints is not None and args.limit_preprints <= 0:
        raise RuntimeError("--limit-preprints must be positive")

    candidates, candidate_fields = read_csv(candidates_path)
    raw_rows, _ = read_csv(raw_path)
    raw_by_key = unique_raw_records(raw_rows)

    if args.all_unresolved:
        mapping_path = data_dir / MAPPING_CSV
        mapping_rows, _ = read_csv(mapping_path)
        selected_preprint_keys = unresolved_preprint_keys(raw_by_key, mapping_rows)
    else:
        selected_preprint_keys = sorted(
            {
                clean_text(row.get("preprint_key"))
                for row in candidates
                if clean_text(row.get("preprint_key"))
            }
        )

    arxiv_ids_by_key: dict[str, str] = {}
    missing_arxiv_keys: list[str] = []
    for key in selected_preprint_keys:
        arxiv_id = arxiv_id_from_raw_row(raw_by_key.get(key, {}))
        if arxiv_id:
            arxiv_ids_by_key[key] = arxiv_id
        else:
            missing_arxiv_keys.append(key)

    all_ids = sorted(set(arxiv_ids_by_key.values()))
    selected_ids = all_ids[: args.limit_preprints] if args.limit_preprints is not None else all_ids
    selected_set = set(selected_ids)

    cache = load_cache(cache_path)
    if args.refresh:
        to_fetch = list(selected_ids)
    else:
        to_fetch = [arxiv_id for arxiv_id in selected_ids if arxiv_id not in cache]

    print("arXiv publication-candidate enrichment")
    print("--------------------------------------")
    print(f"Review candidate pairs:              {len(candidates)}")
    if args.all_unresolved:
        print(f"Unresolved CoRR preprints selected:  {len(arxiv_ids_by_key) + len(missing_arxiv_keys)}")
    else:
        print(f"Unique candidate preprints:          {len(arxiv_ids_by_key) + len(missing_arxiv_keys)}")
    print(f"Exact arXiv IDs extracted from DBLP: {len(arxiv_ids_by_key)}")
    if missing_arxiv_keys:
        label = "Unresolved preprints" if args.all_unresolved else "Candidate preprints"
        print(f"{label} without arXiv ID: {len(missing_arxiv_keys)}")
    print(f"Selected arXiv IDs:                  {len(selected_ids)}")
    print(f"Already cached:                      {sum(1 for arxiv_id in selected_ids if arxiv_id in cache)}")
    print(f"API fetches needed:                  {len(to_fetch)} IDs")

    if args.offline and to_fetch:
        print("Offline mode: missing metadata will remain uncached.")
    elif to_fetch:
        batches = list(chunks(to_fetch, args.batch_size))
        for index, batch in enumerate(batches, start=1):
            print(f"Fetching arXiv batch {index}/{len(batches)} ({len(batch)} IDs)...")
            returned = fetch_arxiv_batch(batch)
            for arxiv_id in batch:
                cache[arxiv_id] = returned.get(
                    arxiv_id,
                    {
                        "arxiv_id": arxiv_id,
                        "status": "not_found",
                    },
                )
            save_cache(cache_path, cache)
            if index < len(batches) and args.delay:
                time.sleep(args.delay)

    enriched = enrich_candidate_rows(
        candidates,
        raw_by_key,
        cache,
        requested_ids=selected_set,
    )
    extra_fields = [
        "arxiv_id",
        "arxiv_metadata_status",
        "arxiv_title",
        "arxiv_authors_json",
        "arxiv_published",
        "arxiv_updated",
        "arxiv_doi",
        "arxiv_journal_ref",
        "arxiv_comment",
        "arxiv_primary_category",
        "arxiv_categories_json",
        "arxiv_abs_url",
        "published_venue_label",
        "published_booktitle",
        "published_journal",
        "published_dois_json",
        "arxiv_doi_matches_published",
        "arxiv_current_title_matches_published",
        "arxiv_evidence",
    ]
    write_csv(output_path, enriched, [*candidate_fields, *extra_fields])

    found = sum(1 for row in enriched if row["arxiv_metadata_status"] == "found")
    with_doi = sum(1 for row in enriched if row["arxiv_doi"])
    with_journal_ref = sum(1 for row in enriched if row["arxiv_journal_ref"])
    with_comment = sum(1 for row in enriched if row["arxiv_comment"])
    doi_matches = sum(1 for row in enriched if row["arxiv_doi_matches_published"] == "1")
    title_matches = sum(1 for row in enriched if row["arxiv_current_title_matches_published"] == "1")
    either = sum(1 for row in enriched if row["arxiv_evidence"])

    print()
    print(f"Candidate rows with arXiv metadata:      {found}")
    print(f"Candidate rows with arXiv DOI:           {with_doi}")
    print(f"Candidate rows with journal-ref:         {with_journal_ref}")
    print(f"Candidate rows with comments:            {with_comment}")
    print(f"Exact arXiv DOI -> published DOI matches:{doi_matches:>7}")
    print(f"Current arXiv title -> published matches:{title_matches:>7}")
    print(f"Rows with either strong audit signal:    {either}")
    print()
    print(f"Wrote: {output_path}")
    print(f"Cache: {cache_path}")
    print("This enrichment step changed no decisions; rerun the resolver to apply eligible evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
