#!/usr/bin/env python3
"""Resolve DBLP publication versions into conservative work-level entities.

DBLP remains the canonical publication corpus. The resolver first applies
high-confidence DBLP-native identity evidence and can then consume cached arXiv
metadata as supporting evidence for unresolved CoRR preprints. arXiv never adds
a publication on its own; every target must already exist as an archival DBLP
record.

DBLP-native automatic evidence:

- an explicit DBLP ``<rel type="versionOf">`` relationship, including multiple
  concrete versions that point to the same abstract parent; or
- an identical DOI attached to more than one DBLP record; or
- a CoRR preprint and an unambiguous archival DBLP destination with the same
  normalized title, exactly the same normalized author set, and a plausible
  publication-year gap; or
- a one-to-one CoRR/preprint archival pair where the preprint title differs only
  by a literal trailing ``(Extended Version)`` or ``(full version)`` marker.

Optional cached arXiv evidence adds only two more conservative rules:

- an arXiv DOI that resolves to exactly one archival DBLP record and shares at
  least one DBLP author with the preprint; or
- the current arXiv title exactly matching one globally unique archival DBLP
  title with the same complete DBLP author set, within the same 0-2 year window.

If DOI and current-title evidence disagree, the resolver abstains. Journal-ref,
comments, abstracts and fuzzy title similarity remain audit-only.

Title/author similarity is deliberately *not* used for automatic merges. It is
used only to create an audit CSV of likely renamed preprint/published-version
pairs for manual inspection, and those candidates must still have the same
complete normalized author set and a plausible publication-year gap. Exact
title/author signatures with multiple archival publications are also audit-only
so that a preprint cannot transitively collapse distinct archival records.

Outputs
-------
- data/dblp_publication_entities_for_db_people.csv
    One row per resolved DB person and work entity. If the person appears on
    multiple versions, the best version that the person actually authored is
    used for display/counting (main ML > other published version > preprint).
- data/publication_entity_resolution.csv
    One row per DBLP publication record with its work ID and component metadata.
- data/reports/publication_entity_candidates.csv
    Unmerged preprint/published pairs suggested only for review.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

try:
    from .enrich_reviewers_with_dblp import DEFAULT_DATA_DIR, clean_text
    from .publication_arxiv import arxiv_id_from_publication_values, normalize_arxiv_id
except ImportError:  # Running as ``python scripts/resolve_publication_entities.py``
    from enrich_reviewers_with_dblp import DEFAULT_DATA_DIR, clean_text
    from publication_arxiv import arxiv_id_from_publication_values, normalize_arxiv_id

RAW_PUBLICATIONS_CSV = "dblp_publications_for_db_people.csv"
OUTPUT_CSV = "dblp_publication_entities_for_db_people.csv"
MAPPING_CSV = "publication_entity_resolution.csv"
CANDIDATES_CSV = "publication_entity_candidates.csv"
ARXIV_CACHE_JSON = "arxiv_candidate_metadata_cache.json"
ARXIV_CACHE_VERSION = 1
RESOLUTION_VERSION = "7"

MAX_PREPRINT_TO_PUBLISHED_YEARS = 2
REVIEW_TITLE_SEQUENCE_THRESHOLD = 0.65
REVIEW_TITLE_TOKEN_THRESHOLD = 0.45

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)
TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")
PREPRINT_VERSION_MARKER_RE = re.compile(
    r"\s*\((?:extended version|full version)\)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PublicationRecord:
    key: str
    title: str
    year: int
    scope: str
    venue: str
    venue_label: str
    record_type: str
    publtype: str
    booktitle: str
    journal: str
    url: str
    authors: tuple[str, ...]
    ees: tuple[str, ...]
    rels: tuple[dict[str, str], ...]
    dois: tuple[str, ...]

    @property
    def is_preprint(self) -> bool:
        low_key = self.key.casefold()
        return low_key.startswith("journals/corr/") or self.journal.casefold() == "corr"

    @property
    def is_archival_publication(self) -> bool:
        return (
            not self.is_preprint
            and self.record_type in {"article", "inproceedings"}
            and self.publtype.casefold() != "informal"
        )


class UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}
        self.rank = {value: 0 for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        a = self.find(left)
        b = self.find(right)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(item) for item in parsed if clean_text(item)]


def json_dict_list(value: Any) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    result: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                str(key): clean_text(val)
                for key, val in item.items()
                if clean_text(val)
            }
        )
    return result


def normalize_doi(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    match = DOI_RE.search(unquote(value))
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}").casefold()


def dois_from_row(row: dict[str, str]) -> tuple[str, ...]:
    values = [row.get("doi", ""), *json_list(row.get("ees_json", "")), row.get("url", "")]
    return tuple(sorted({doi for value in values if (doi := normalize_doi(value))}))


def normalize_relation_target(value: str) -> str:
    """Normalize a DBLP rel target while preserving non-DBLP parent URIs."""
    value = clean_text(value)
    if not value:
        return ""

    if value.startswith("dblp:"):
        return value[len("dblp:") :].strip("/")

    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.casefold()
        path = unquote(parsed.path)
        if "dblp" in host and "/rec/" in path:
            key = path.split("/rec/", 1)[1].strip("/")
            if key.endswith(".html"):
                key = key[:-5]
            return key
        # For abstract version-parent URIs, stable URI identity is useful even
        # when the parent is not one of our paper-like DBLP records.
        return value.rstrip("/")

    return value.strip("/")


def is_version_relation(rel: dict[str, str]) -> bool:
    tokens = clean_text(rel.get("type", "")).split()
    return any(token.casefold() == "versionof" for token in tokens)


def record_from_row(row: dict[str, str]) -> PublicationRecord | None:
    key = clean_text(row.get("dblp_key"))
    title = clean_text(row.get("title"))
    if not key or not title:
        return None
    try:
        year = int(row.get("year") or "")
    except ValueError:
        return None
    return PublicationRecord(
        key=key,
        title=title,
        year=year,
        scope="main" if clean_text(row.get("scope")).casefold() == "main" else "side",
        venue=clean_text(row.get("venue")),
        venue_label=clean_text(row.get("venue_label")),
        record_type=clean_text(row.get("record_type")),
        publtype=clean_text(row.get("publtype")),
        booktitle=clean_text(row.get("booktitle")),
        journal=clean_text(row.get("journal")),
        url=clean_text(row.get("url")),
        authors=tuple(json_list(row.get("authors_json", ""))),
        ees=tuple(json_list(row.get("ees_json", ""))),
        rels=tuple(json_dict_list(row.get("rels_json", ""))),
        dois=dois_from_row(row),
    )


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        raise RuntimeError(
            f"Missing {path.name}. Run scripts/scan_dblp_publications.py first."
        )
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise RuntimeError(f"Missing CSV header in {path}")
        rows = [dict(row) for row in reader]
        fields = list(reader.fieldnames)
    return rows, fields


def unique_records(rows: list[dict[str, str]]) -> dict[str, PublicationRecord]:
    records: dict[str, PublicationRecord] = {}
    for row in rows:
        record = record_from_row(row)
        if record is None:
            continue
        old = records.get(record.key)
        if old is not None and (
            old.title != record.title
            or old.year != record.year
            or old.scope != record.scope
        ):
            raise RuntimeError(
                f"Conflicting metadata for DBLP publication key {record.key}"
            )
        records[record.key] = record
    return records


def build_components(
    records: dict[str, PublicationRecord],
    arxiv_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    union = UnionFind(records)
    edges: list[tuple[str, str, str]] = []

    # A version relation may point directly to another publication key or to a
    # shared abstract parent URI. Group all concrete records sharing that parent.
    version_sources: dict[str, list[str]] = defaultdict(list)
    for record in records.values():
        for rel in record.rels:
            if not is_version_relation(rel):
                continue
            target = normalize_relation_target(rel.get("target") or rel.get("uri") or rel.get("text") or "")
            if not target:
                continue
            version_sources[target].append(record.key)
            if target in records:
                union.union(record.key, target)
                edges.append((record.key, target, "dblp_rel_versionOf"))

    for target, keys in version_sources.items():
        unique_keys = sorted(set(keys))
        if len(unique_keys) > 1:
            first = unique_keys[0]
            for other in unique_keys[1:]:
                union.union(first, other)
                edges.append((first, other, "dblp_rel_versionOf"))

    doi_groups: dict[str, list[str]] = defaultdict(list)
    for record in records.values():
        for doi in record.dois:
            doi_groups[doi].append(record.key)
    for keys in doi_groups.values():
        unique_keys = sorted(set(keys))
        if len(unique_keys) > 1:
            first = unique_keys[0]
            for other in unique_keys[1:]:
                union.union(first, other)
                edges.append((first, other, "shared_doi"))

    # Native relations/DOIs may establish archival equivalence. Weaker
    # preprint rules must never bridge two such archival components. Track the
    # current roots after every union: union-by-rank can retain a preprint root.
    archival_roots = {
        union.find(key)
        for key, record in records.items()
        if record.is_archival_publication
    }

    def merge_preprint(preprint_key: str, published_key: str) -> bool:
        preprint_root = union.find(preprint_key)
        published_root = union.find(published_key)
        if preprint_root == published_root:
            return True
        if preprint_root in archival_roots:
            return False
        contains_archival = published_root in archival_roots
        union.union(preprint_root, published_root)
        archival_roots.discard(preprint_root)
        archival_roots.discard(published_root)
        if contains_archival:
            archival_roots.add(union.find(preprint_root))
        return True

    # Strong DBLP-only fallback for the common case where DBLP stores a CoRR
    # preprint and an archival article/proceedings record but no explicit
    # version relation. Candidate edges require an exact normalized title, an
    # exact normalized author set, and publication no earlier than the preprint
    # and no more than two years later.
    #
    # We auto-merge only candidate graph components with exactly one archival
    # destination. This prevents transitive over-merging such as
    # CoRR -> conference + journal while still allowing multiple preprints to
    # collapse into one archival record, or repeated title/author signatures
    # years apart to resolve as separate unambiguous components.
    for preprints, published, candidate_edges in exact_title_author_candidate_components(records):
        if len(published) != 1:
            continue
        published_key = published[0]
        for preprint_key, edge_published_key in candidate_edges:
            if edge_published_key != published_key:
                continue
            if not merge_preprint(preprint_key, published_key):
                continue
            edges.append(
                (
                    preprint_key,
                    published_key,
                    "exact_title_authors_preprint_published",
                )
            )

    # Tiny, deliberately narrow second fallback: a literal trailing preprint
    # marker may be ignored only for an otherwise exact one-to-one pair.
    for preprints, published, candidate_edges in explicit_preprint_marker_candidate_components(records):
        if len(preprints) != 1 or len(published) != 1 or len(candidate_edges) != 1:
            continue
        preprint_key, published_key = candidate_edges[0]
        if union.find(preprint_key) == union.find(published_key):
            continue
        if not merge_preprint(preprint_key, published_key):
            continue
        edges.append(
            (
                preprint_key,
                published_key,
                "explicit_preprint_version_marker",
            )
        )

    # External evidence is deliberately last and may only resolve a preprint
    # whose DBLP-native component does not already contain an archival record.
    # This prevents arXiv metadata from overriding or transitively broadening a
    # stronger DBLP-native merge.
    if arxiv_metadata:
        for preprint_key, published_key, evidence_bases in arxiv_evidence_links(
            records, arxiv_metadata
        ):
            if union.find(preprint_key) == union.find(published_key):
                continue
            if not merge_preprint(preprint_key, published_key):
                continue
            for basis in evidence_bases:
                edges.append((preprint_key, published_key, basis))

    components: dict[str, list[str]] = defaultdict(list)
    for key in records:
        components[union.find(key)].append(key)
    components = {
        root: sorted(keys) for root, keys in components.items()
    }

    bases: dict[str, set[str]] = defaultdict(set)
    for left, right, basis in edges:
        root = union.find(left)
        if union.find(right) == root:
            bases[root].add(basis)
    return components, bases


def record_priority(record: PublicationRecord) -> tuple[int, int, int, int, str]:
    """Lower is better. Never prefer a version a person did not author."""
    type_rank = 0 if record.record_type in {"article", "inproceedings"} else 1
    return (
        0 if record.scope == "main" else 1,
        0 if not record.is_preprint else 1,
        type_rank,
        -record.year,
        record.key.casefold(),
    )


def canonical_keys(
    records: dict[str, PublicationRecord],
    components: dict[str, list[str]],
) -> dict[str, str]:
    return {
        root: min(keys, key=lambda key: record_priority(records[key]))
        for root, keys in components.items()
    }


def publication_version_metadata(
    records: dict[str, PublicationRecord],
    member_keys: Iterable[str],
) -> list[dict[str, Any]]:
    """Return compact, auditable metadata for every DBLP record in a work.

    The website uses this to expose the concrete preprint/archival records that
    were collapsed behind the displayed work instead of showing only a record
    count.  Ordering mirrors canonical/display priority, so a main-conference
    record naturally appears before side publications and preprints.
    """

    versions: list[dict[str, Any]] = []
    for key in sorted(member_keys, key=lambda value: record_priority(records[value])):
        record = records[key]
        arxiv_id = arxiv_id_for_record(record) if record.is_preprint else ""
        versions.append(
            {
                "dblpKey": record.key,
                "title": record.title,
                "year": record.year,
                "scope": record.scope,
                "venue": record.venue,
                "venueLabel": record.venue_label,
                "recordType": record.record_type,
                "publtype": record.publtype,
                "booktitle": record.booktitle,
                "journal": record.journal,
                "url": record.url,
                "ees": list(record.ees),
                "isPreprint": record.is_preprint,
                **({"arxivId": arxiv_id} if arxiv_id else {}),
            }
        )
    return versions


def component_lookup(components: dict[str, list[str]]) -> dict[str, str]:
    return {
        key: root
        for root, keys in components.items()
        for key in keys
    }


def normalized_author_set(record: PublicationRecord) -> set[str]:
    return {
        " ".join(author.casefold().split())
        for author in record.authors
        if clean_text(author)
    }


def normalized_title(title: str) -> str:
    return " ".join(TITLE_TOKEN_RE.findall(title.casefold()))


def title_token_set(title: str) -> set[str]:
    return set(TITLE_TOKEN_RE.findall(title.casefold()))


def normalized_title_without_preprint_version_marker(title: str) -> str:
    """Return the normalized base title only for a literal trailing marker.

    This intentionally recognizes only ``(Extended Version)`` and
    ``(full version)`` when the marker itself terminates the preprint title.
    Broader wording and punctuation variants remain review-only.
    """
    raw = clean_text(title)
    stripped = PREPRINT_VERSION_MARKER_RE.sub("", raw)
    if stripped == raw:
        return ""
    return normalized_title(stripped)


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def load_arxiv_metadata_cache(path: Path) -> dict[str, dict[str, Any]]:
    """Load only found arXiv metadata records from the local evidence cache."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != ARXIV_CACHE_VERSION:
        return {}
    records = payload.get("records")
    if not isinstance(records, dict):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for stored_id, record in records.items():
        if not isinstance(record, dict):
            continue
        if clean_text(record.get("status")).casefold() != "found":
            continue
        arxiv_id = normalize_arxiv_id(stored_id)
        if arxiv_id:
            result[arxiv_id] = record
    return result


def arxiv_id_for_record(record: PublicationRecord) -> str:
    return arxiv_id_from_publication_values(
        url=record.url,
        ees=record.ees,
        dblp_key=record.key,
    )


def arxiv_evidence_links(
    records: dict[str, PublicationRecord],
    arxiv_metadata: dict[str, dict[str, Any]],
) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return high-confidence arXiv-supported preprint -> DBLP archival links.

    DOI evidence requires a unique archival DBLP DOI target plus at least one
    shared DBLP author. Current-title evidence requires the current arXiv title
    and the preprint's complete DBLP author set to identify one globally unique
    archival DBLP record, with publication no more than two years later. If the
    two evidence types point at different targets, the resolver abstains.
    """
    metadata = {
        normalized: value
        for key, value in arxiv_metadata.items()
        if (normalized := normalize_arxiv_id(key)) and isinstance(value, dict)
    }

    doi_targets: dict[str, set[str]] = defaultdict(set)
    title_author_targets: dict[tuple[str, tuple[str, ...]], set[str]] = defaultdict(set)
    for record in records.values():
        if not record.is_archival_publication:
            continue
        for doi in record.dois:
            doi_targets[doi].add(record.key)
        title = normalized_title(record.title)
        authors = tuple(sorted(normalized_author_set(record)))
        if title and authors:
            title_author_targets[(title, authors)].add(record.key)

    links: list[tuple[str, str, tuple[str, ...]]] = []
    for preprint in sorted(
        (record for record in records.values() if record.is_preprint),
        key=lambda record: record.key,
    ):
        arxiv_id = arxiv_id_for_record(preprint)
        record_metadata = metadata.get(arxiv_id) if arxiv_id else None
        if not record_metadata:
            continue
        if clean_text(record_metadata.get("status")).casefold() not in {"", "found"}:
            continue

        current_title = normalized_title(clean_text(record_metadata.get("title")))
        preprint_author_set = normalized_author_set(preprint)
        preprint_authors = tuple(sorted(preprint_author_set))

        doi_target = ""
        arxiv_doi = normalize_doi(str(record_metadata.get("doi") or ""))
        if arxiv_doi:
            candidates = sorted(doi_targets.get(arxiv_doi, set()))
            if len(candidates) == 1:
                candidate = records[candidates[0]]
                candidate_author_set = normalized_author_set(candidate)
                shared_authors = preprint_author_set & candidate_author_set
                mutual_majority_authors = bool(
                    preprint_author_set
                    and candidate_author_set
                    and len(shared_authors) / len(preprint_author_set) >= 0.5
                    and len(shared_authors) / len(candidate_author_set) >= 0.5
                )
                exact_current_title = bool(
                    current_title and current_title == normalized_title(candidate.title)
                )
                # DOI metadata can occasionally point at a proceedings-level or
                # otherwise broader publication that merely contains the preprint
                # authors. Accept the unique DOI target only when the current
                # arXiv title matches exactly or the author sets overlap by at
                # least half in both directions.
                if exact_current_title or mutual_majority_authors:
                    doi_target = candidate.key

        title_target = ""
        if current_title and preprint_authors:
            candidates = sorted(
                title_author_targets.get((current_title, preprint_authors), set())
            )
            if len(candidates) == 1:
                candidate = records[candidates[0]]
                year_gap = candidate.year - preprint.year
                if 0 <= year_gap <= MAX_PREPRINT_TO_PUBLISHED_YEARS:
                    title_target = candidate.key

        if doi_target and title_target and doi_target != title_target:
            continue
        target = doi_target or title_target
        if not target:
            continue
        bases: list[str] = []
        if doi_target:
            bases.append("arxiv_doi")
        if title_target:
            bases.append("arxiv_current_title_exact")
        links.append((preprint.key, target, tuple(bases)))

    return links


def exact_title_author_candidate_components(
    records: dict[str, PublicationRecord],
) -> list[tuple[list[str], list[str], list[tuple[str, str]]]]:
    """Return exact-title/author candidate graph components.

    Edges connect CoRR preprints to archival publications only when the archival
    year is from the preprint year through two years later. Keeping the graph
    structure explicit lets the resolver distinguish one unambiguous archival
    destination from a preprint that bridges multiple archival publications.
    """
    signature_groups: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    for record in records.values():
        title = normalized_title(record.title)
        authors = tuple(sorted(normalized_author_set(record)))
        if title and authors:
            signature_groups[(title, authors)].append(record.key)

    result: list[tuple[list[str], list[str], list[tuple[str, str]]]] = []
    for keys in signature_groups.values():
        preprints = sorted(key for key in keys if records[key].is_preprint)
        published = sorted(key for key in keys if records[key].is_archival_publication)
        if not preprints or not published:
            continue

        adjacency: dict[str, set[str]] = defaultdict(set)
        edges: list[tuple[str, str]] = []
        for preprint_key in preprints:
            preprint = records[preprint_key]
            for published_key in published:
                published_record = records[published_key]
                year_gap = published_record.year - preprint.year
                if not (0 <= year_gap <= MAX_PREPRINT_TO_PUBLISHED_YEARS):
                    continue
                adjacency[preprint_key].add(published_key)
                adjacency[published_key].add(preprint_key)
                edges.append((preprint_key, published_key))

        unseen = set(adjacency)
        while unseen:
            first = min(unseen)
            stack = [first]
            nodes: set[str] = set()
            while stack:
                node = stack.pop()
                if node in nodes:
                    continue
                nodes.add(node)
                unseen.discard(node)
                stack.extend(sorted(adjacency[node] - nodes, reverse=True))

            component_preprints = sorted(
                key for key in nodes if records[key].is_preprint
            )
            component_published = sorted(
                key for key in nodes if records[key].is_archival_publication
            )
            component_edges = sorted(
                (preprint_key, published_key)
                for preprint_key, published_key in edges
                if preprint_key in nodes and published_key in nodes
            )
            result.append(
                (component_preprints, component_published, component_edges)
            )

    return result


def explicit_preprint_marker_candidate_components(
    records: dict[str, PublicationRecord],
) -> list[tuple[list[str], list[str], list[tuple[str, str]]]]:
    """Return candidate components for explicit preprint-version markers.

    Only CoRR preprints whose title literally ends with ``(Extended Version)``
    or ``(full version)`` participate. After removing that marker, the
    normalized title and complete normalized author set must exactly match an
    archival publication within the usual 0-2 year window.
    """
    preprint_groups: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)
    published_groups: dict[tuple[str, tuple[str, ...]], list[str]] = defaultdict(list)

    for record in records.values():
        authors = tuple(sorted(normalized_author_set(record)))
        if not authors:
            continue
        if record.is_preprint:
            title = normalized_title_without_preprint_version_marker(record.title)
            if title:
                preprint_groups[(title, authors)].append(record.key)
        elif record.is_archival_publication:
            title = normalized_title(record.title)
            if title:
                published_groups[(title, authors)].append(record.key)

    result: list[tuple[list[str], list[str], list[tuple[str, str]]]] = []
    for signature in sorted(set(preprint_groups) & set(published_groups)):
        preprints = sorted(preprint_groups[signature])
        published = sorted(published_groups[signature])
        adjacency: dict[str, set[str]] = defaultdict(set)
        edges: list[tuple[str, str]] = []
        for preprint_key in preprints:
            preprint = records[preprint_key]
            for published_key in published:
                published_record = records[published_key]
                year_gap = published_record.year - preprint.year
                if not (0 <= year_gap <= MAX_PREPRINT_TO_PUBLISHED_YEARS):
                    continue
                adjacency[preprint_key].add(published_key)
                adjacency[published_key].add(preprint_key)
                edges.append((preprint_key, published_key))

        unseen = set(adjacency)
        while unseen:
            first = min(unseen)
            stack = [first]
            nodes: set[str] = set()
            while stack:
                node = stack.pop()
                if node in nodes:
                    continue
                nodes.add(node)
                unseen.discard(node)
                stack.extend(sorted(adjacency[node] - nodes, reverse=True))

            component_preprints = sorted(
                key for key in nodes if records[key].is_preprint
            )
            component_published = sorted(
                key for key in nodes if records[key].is_archival_publication
            )
            component_edges = sorted(
                (preprint_key, published_key)
                for preprint_key, published_key in edges
                if preprint_key in nodes and published_key in nodes
            )
            result.append(
                (component_preprints, component_published, component_edges)
            )

    return result


def candidate_pairs(
    rows: list[dict[str, str]],
    records: dict[str, PublicationRecord],
    key_to_root: dict[str, str],
    bases: dict[str, set[str]],
) -> list[dict[str, Any]]:
    ambiguous_exact_pairs = {
        pair
        for _preprints, published, candidate_edges
        in exact_title_author_candidate_components(records)
        if len(published) > 1
        for pair in candidate_edges
    }

    by_pid: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        pid = clean_text(row.get("dblp_pid"))
        key = clean_text(row.get("dblp_key"))
        if pid and key in records:
            by_pid[pid].add(key)

    pair_people: dict[tuple[str, str], set[str]] = defaultdict(set)
    for pid, keys in by_pid.items():
        preprints = [key for key in keys if records[key].is_preprint]
        published = [key for key in keys if records[key].is_archival_publication]
        for preprint_key in preprints:
            preprint_root = key_to_root[preprint_key]
            if bases.get(preprint_root, set()) & {
                "arxiv_doi",
                "arxiv_current_title_exact",
            }:
                # Strong external evidence has already selected this preprint's
                # archival destination; do not keep weaker alternative pairs in
                # the manual-review queue.
                continue
            preprint = records[preprint_key]
            for published_key in published:
                published_record = records[published_key]
                if key_to_root[preprint_key] == key_to_root[published_key]:
                    continue
                year_gap = published_record.year - preprint.year
                if not (0 <= year_gap <= MAX_PREPRINT_TO_PUBLISHED_YEARS):
                    continue
                pair_people[(preprint_key, published_key)].add(pid)

    candidates: list[dict[str, Any]] = []
    for (preprint_key, published_key), people in sorted(pair_people.items()):
        preprint = records[preprint_key]
        published = records[published_key]
        pre_authors = normalized_author_set(preprint)
        pub_authors = normalized_author_set(published)
        same_authors = bool(pre_authors) and pre_authors == pub_authors
        if not same_authors:
            continue

        left_title = normalized_title(preprint.title)
        right_title = normalized_title(published.title)
        if not left_title or not right_title:
            continue
        if left_title == right_title:
            if (preprint_key, published_key) not in ambiguous_exact_pairs:
                # Unambiguous exact-title pairs are handled automatically above.
                continue
            candidates.append(
                {
                    "preprint_key": preprint.key,
                    "published_key": published.key,
                    "preprint_year": preprint.year,
                    "published_year": published.year,
                    "preprint_title": preprint.title,
                    "published_title": published.title,
                    "author_jaccard": "1.000",
                    "same_author_set": "1",
                    "title_similarity": "1.000",
                    "title_token_jaccard": "1.000",
                    "shared_target_people": len(people),
                    "reason": "ambiguous_exact_title_multiple_archival_publications",
                }
            )
            continue
        title_similarity = SequenceMatcher(None, left_title, right_title).ratio()
        token_similarity = jaccard(
            title_token_set(preprint.title), title_token_set(published.title)
        )

        qualifies = (
            title_similarity >= REVIEW_TITLE_SEQUENCE_THRESHOLD
            or token_similarity >= REVIEW_TITLE_TOKEN_THRESHOLD
        )
        if not qualifies:
            continue

        candidates.append(
            {
                "preprint_key": preprint.key,
                "published_key": published.key,
                "preprint_year": preprint.year,
                "published_year": published.year,
                "preprint_title": preprint.title,
                "published_title": published.title,
                "author_jaccard": "1.000",
                "same_author_set": "1",
                "title_similarity": f"{title_similarity:.3f}",
                "title_token_jaccard": f"{token_similarity:.3f}",
                "shared_target_people": len(people),
                "reason": "review_only_preprint_published_candidate",
            }
        )
    return candidates


def resolve_rows(
    rows: list[dict[str, str]],
    arxiv_metadata: dict[str, dict[str, Any]] | None = None,
) -> tuple[
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, Any]],
    dict[str, int],
]:
    records = unique_records(rows)
    components, bases = build_components(records, arxiv_metadata=arxiv_metadata)
    canon = canonical_keys(records, components)
    key_to_root = component_lookup(components)

    grouped_rows: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        pid = clean_text(row.get("dblp_pid"))
        key = clean_text(row.get("dblp_key"))
        if not pid or key not in key_to_root:
            continue
        grouped_rows[(pid, key_to_root[key])].append(row)

    resolved: list[dict[str, str]] = []
    for (pid, root), person_rows in sorted(grouped_rows.items()):
        # Only choose among versions this person actually authored. This avoids
        # attributing a final paper to someone who appeared only on a preprint.
        selected = min(
            person_rows,
            key=lambda row: record_priority(records[clean_text(row.get("dblp_key"))]),
        )
        result = dict(selected)
        member_keys = components[root]
        person_keys = sorted(
            {clean_text(row.get("dblp_key")) for row in person_rows if clean_text(row.get("dblp_key"))}
        )
        canonical_key = canon[root]
        result.update(
            {
                "work_id": f"dblp-work:{canonical_key}",
                "canonical_dblp_key": canonical_key,
                "version_count": str(len(member_keys)),
                "version_dblp_keys": json.dumps(member_keys, ensure_ascii=False),
                "publication_versions_json": json.dumps(
                    publication_version_metadata(records, member_keys),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "person_version_count": str(len(person_keys)),
                "person_version_dblp_keys": json.dumps(person_keys, ensure_ascii=False),
                "publication_entity_basis": ";".join(sorted(bases.get(root, set())))
                or "single_record",
                "publication_entity_version": RESOLUTION_VERSION,
            }
        )
        resolved.append(result)

    mapping: list[dict[str, str]] = []
    for root, member_keys in sorted(components.items(), key=lambda item: canon[item[0]]):
        canonical_key = canon[root]
        basis = ";".join(sorted(bases.get(root, set()))) or "single_record"
        work_id = f"dblp-work:{canonical_key}"
        for key in member_keys:
            mapping.append(
                {
                    "dblp_key": key,
                    "work_id": work_id,
                    "canonical_dblp_key": canonical_key,
                    "is_canonical": "1" if key == canonical_key else "0",
                    "component_size": str(len(member_keys)),
                    "resolution_basis": basis,
                    "version_dblp_keys": json.dumps(member_keys, ensure_ascii=False),
                    "canonical_title": records[canonical_key].title,
                    "canonical_year": str(records[canonical_key].year),
                }
            )

    candidates = candidate_pairs(rows, records, key_to_root, bases)
    stats = {
        "raw_links": len(rows),
        "unique_records": len(records),
        "work_entities": len(components),
        "merged_records": len(records) - len(components),
        "resolved_person_work_links": len(resolved),
        "review_candidates": len(candidates),
        "ambiguous_exact_title_candidates": sum(
            1
            for candidate in candidates
            if candidate["reason"]
            == "ambiguous_exact_title_multiple_archival_publications"
        ),
        "renamed_title_candidates": sum(
            1
            for candidate in candidates
            if candidate["reason"] == "review_only_preprint_published_candidate"
        ),
        "version_relation_components": sum(
            1 for root in components if "dblp_rel_versionOf" in bases.get(root, set())
        ),
        "shared_doi_components": sum(
            1 for root in components if "shared_doi" in bases.get(root, set())
        ),
        "exact_title_author_components": sum(
            1
            for root in components
            if "exact_title_authors_preprint_published" in bases.get(root, set())
        ),
        "explicit_preprint_marker_components": sum(
            1
            for root in components
            if "explicit_preprint_version_marker" in bases.get(root, set())
        ),
        "arxiv_evidence_components": sum(
            1
            for root in components
            if bases.get(root, set()) & {"arxiv_doi", "arxiv_current_title_exact"}
        ),
        "arxiv_doi_components": sum(
            1 for root in components if "arxiv_doi" in bases.get(root, set())
        ),
        "arxiv_current_title_components": sum(
            1
            for root in components
            if "arxiv_current_title_exact" in bases.get(root, set())
        ),
    }
    return resolved, mapping, candidates, stats


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--no-arxiv-evidence",
        action="store_true",
        help="Ignore the local arXiv metadata cache and run DBLP-only resolution.",
    )
    parser.add_argument(
        "--arxiv-cache",
        type=Path,
        default=None,
        help="Override the local arXiv metadata cache path.",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    input_path = (args.input or (data_dir / RAW_PUBLICATIONS_CSV)).resolve()
    output_path = (args.output or (data_dir / OUTPUT_CSV)).resolve()
    mapping_path = data_dir / MAPPING_CSV
    reports_dir = data_dir / "reports"
    candidates_path = reports_dir / CANDIDATES_CSV
    arxiv_cache_path = (args.arxiv_cache or (reports_dir / ARXIV_CACHE_JSON)).resolve()

    try:
        rows, input_fields = load_rows(input_path)
        arxiv_metadata = (
            {} if args.no_arxiv_evidence else load_arxiv_metadata_cache(arxiv_cache_path)
        )
        resolved, mapping, candidates, stats = resolve_rows(
            rows, arxiv_metadata=arxiv_metadata
        )
    except (OSError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    extra_fields = [
        "work_id",
        "canonical_dblp_key",
        "version_count",
        "version_dblp_keys",
        "publication_versions_json",
        "person_version_count",
        "person_version_dblp_keys",
        "publication_entity_basis",
        "publication_entity_version",
    ]
    output_fields = input_fields + [field for field in extra_fields if field not in input_fields]
    write_csv(output_path, resolved, output_fields)
    write_csv(
        mapping_path,
        mapping,
        [
            "dblp_key",
            "work_id",
            "canonical_dblp_key",
            "is_canonical",
            "component_size",
            "resolution_basis",
            "version_dblp_keys",
            "canonical_title",
            "canonical_year",
        ],
    )
    write_csv(
        candidates_path,
        candidates,
        [
            "preprint_key",
            "published_key",
            "preprint_year",
            "published_year",
            "preprint_title",
            "published_title",
            "author_jaccard",
            "same_author_set",
            "title_similarity",
            "title_token_jaccard",
            "shared_target_people",
            "reason",
        ],
    )

    print("\nDBLP publication entity resolution")
    print("----------------------------------")
    print(f"Raw person/publication links:     {stats['raw_links']}")
    print(f"Unique DBLP publication records:  {stats['unique_records']}")
    print(f"Resolved work entities:           {stats['work_entities']}")
    print(f"Automatically merged records:     {stats['merged_records']}")
    print(f"  versionOf components:           {stats['version_relation_components']}")
    print(f"  shared-DOI components:          {stats['shared_doi_components']}")
    print(f"  exact title/author components:  {stats['exact_title_author_components']}")
    print(f"  explicit preprint markers:      {stats['explicit_preprint_marker_components']}")
    print(f"  arXiv evidence components:      {stats['arxiv_evidence_components']}")
    print(f"    arXiv DOI:                    {stats['arxiv_doi_components']}")
    print(f"    arXiv current-title exact:    {stats['arxiv_current_title_components']}")
    if args.no_arxiv_evidence:
        print("  arXiv cache:                    disabled")
    elif arxiv_metadata:
        print(f"  arXiv cache records loaded:     {len(arxiv_metadata)}")
    else:
        print("  arXiv cache records loaded:     0")
    print(f"Person/work links:                {stats['resolved_person_work_links']}")
    print(f"Review-only candidate pairs:      {stats['review_candidates']}")
    print(f"  ambiguous exact-title pairs:    {stats['ambiguous_exact_title_candidates']}")
    print(f"  renamed-title pairs:            {stats['renamed_title_candidates']}")
    print(f"\nWrote: {output_path}")
    print(f"Wrote: {mapping_path}")
    print(f"Wrote: {candidates_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
