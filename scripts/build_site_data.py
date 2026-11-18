#!/usr/bin/env python3
"""Build the static website dataset from resolved DB service + DBLP publications.

Required inputs in --data-dir:
    entity_resolution.sqlite
    dblp_publication_entities_for_db_people.csv

The publication-entity CSV is produced by ``resolve_publication_entities.py``
from the DBLP-only scan. It collapses only high-confidence DBLP version/DOI
relationships before the website classifies works as:

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
import hashlib
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timezone
from urllib.parse import urlparse
from pathlib import Path
from typing import Any, Iterable

try:
    from .automatic_affiliations import OUTPUT_FILENAME as AUTOMATIC_AFFILIATIONS_JSON, POLICY_VERSION as AFFILIATION_POLICY_VERSION, apply_automatic_evidence, evidence_refreshed_at
    from .display_cleaning import (
        clean_affiliation_label,
        clean_affiliations_with_audit,
        fuzzy_backend,
    )
    from .home_population import write_home_population_summary
    from .site_paths import WEBSITE_DIR, BUILD_EXPORT_DIR
    from .review_provenance import REVIEW_FILES, ATTRIBUTIONS_FILE, load_project_reviews, annotate_project_reviews
    from .focus_population import apply_focus_population, cohort_summary, RULES_PATH as FOCUS_RULES_PATH, EXCLUSIONS_PATH as FOCUS_EXCLUSIONS_PATH
    from .data_coverage import export_coverage, publication_row_issues
    from .publication_arxiv import arxiv_id_from_publication_values
    from .publication_categories import MAPPINGS_PATH, default_category_mappings, matching_category_rule
    from .ror_affiliations import ROR_INDEX_FILENAME, ROR_RELEASE, RorAffiliationIndex
except ImportError:
    from automatic_affiliations import OUTPUT_FILENAME as AUTOMATIC_AFFILIATIONS_JSON, POLICY_VERSION as AFFILIATION_POLICY_VERSION, apply_automatic_evidence, evidence_refreshed_at
    from display_cleaning import (
        clean_affiliation_label,
        clean_affiliations_with_audit,
        fuzzy_backend,
    )
    from home_population import write_home_population_summary
    from site_paths import WEBSITE_DIR, BUILD_EXPORT_DIR
    from review_provenance import REVIEW_FILES, ATTRIBUTIONS_FILE, load_project_reviews, annotate_project_reviews
    from focus_population import apply_focus_population, cohort_summary, RULES_PATH as FOCUS_RULES_PATH, EXCLUSIONS_PATH as FOCUS_EXCLUSIONS_PATH
    from data_coverage import export_coverage, publication_row_issues
    from publication_arxiv import arxiv_id_from_publication_values
    from publication_categories import MAPPINGS_PATH, default_category_mappings, matching_category_rule
    from ror_affiliations import ROR_INDEX_FILENAME, ROR_RELEASE, RorAffiliationIndex

DB_ER = "entity_resolution.sqlite"
RAW_PUBLICATIONS_CSV = "dblp_publications_for_db_people.csv"
PUBLICATIONS_CSV = "dblp_publication_entities_for_db_people.csv"
MAIN_TOTALS_CSV = "dblp_main_ml_venue_year_totals.csv"
ARXIV_CACHE_JSON = "arxiv_candidate_metadata_cache.json"
PUBLICATION_AFFILIATIONS_CSV = "publication_affiliation_evidence.csv"
REVIEWER_DATABASES = (
    "sigmod_reviewers.sqlite",
    "vldb_reviewers.sqlite",
    "icde_reviewers.sqlite",
    "edbt_reviewers.sqlite",
    "cidr_reviewers.sqlite",
)
SCHEMA_VERSION = 20
PROVENANCE_SCHEMA_VERSION = 5
BROWSER_DETAIL_SCHEMA_VERSION = 1
BROWSER_SEARCH_SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_OUTPUT_DIR = WEBSITE_DIR / "data"


PUBLICATION_CLASS_LABELS = {
    "main_ml": "Main ML",
    "database": "Database",
    "other_conference": "Other conference",
    "journal": "Journal",
    "workshop": "Workshop",
    "preprint": "Preprint",
    "other": "Other",
}


SERVICE_PARSER_METHODS = {
    "cidr_reviewers.sqlite": "scrape_cidr_reviewers",
    "edbt_reviewers.sqlite": "scrape_edbt_reviewers",
    "icde_reviewers.sqlite": "scrape_icde_reviewers",
    "sigmod_reviewers.sqlite": "scrape_sigmod_reviewers",
    "vldb_reviewers.sqlite": "scrape_vldb_reviewers",
}

VENUE_PARSER_METHODS = {
    "CIDR": "scrape_cidr_reviewers",
    "EDBT": "scrape_edbt_reviewers",
    "ICDE": "scrape_icde_reviewers",
    "SIGMOD": "scrape_sigmod_reviewers",
    "VLDB": "scrape_vldb_reviewers",
}

IDENTITY_DECISION_RULES = {
    "resolved_unique_exact": {
        "label": "Unique exact DBLP alias",
        "description": "Exactly one local DBLP person identity matched the normalized service-name alias.",
    },
    "resolved_high_precision_unique_variant": {
        "label": "Unique name variant + conservative affiliation",
        "description": "Exactly one first/last-name DBLP variant matched and its affiliation was conservatively equivalent to a service affiliation.",
    },
    "resolved_high_precision_exact_affiliation": {
        "label": "Exact name + unique conservative affiliation",
        "description": "Multiple DBLP identities shared the exact name, but exactly one had conservative affiliation evidence while all competitors had affiliation data.",
    },
    "resolved_ror_unique_variant": {
        "label": "Unique name variant + ROR organization",
        "description": "A unique DBLP name variant and the service affiliation independently canonicalized to the same unambiguous ROR organization.",
    },
    "resolved_ror_exact_affiliation": {
        "label": "Exact name + unique ROR organization",
        "description": "Among exact-name DBLP candidates with ROR coverage, exactly one shared a canonical ROR organization with the service affiliation.",
    },
    "resolved_existing_pid_alias": {
        "label": "Alias propagated to an already-resolved DBLP PID",
        "description": "The service-name spelling had exactly one compatible candidate and that DBLP PID was already resolved elsewhere in the dataset.",
    },
    "resolved_existing_pid_ror_alias": {
        "label": "ROR-backed alias propagated to an existing PID",
        "description": "Exactly one compatible, already-resolved DBLP PID also shared an explicit ROR organization with the service affiliation.",
    },
    "resolved_ror_two_anchor_exact_name": {
        "label": "Exact name + two independent ROR anchors",
        "description": "One exact-name candidate shared at least two distinct ROR organizations supported by independent affiliation observations on both sides.",
    },
    "resolved_affiliation_exact": {
        "label": "Exact name + complete affiliation evidence",
        "description": "One real exact-name candidate matches every recorded service affiliation through exact ROR organization evidence and passes an established matching rule. No other candidate shares a service organization.",
    },
    "resolved_affiliation_repeated_service": {
        "label": "Exact name + affiliations on separate service pages",
        "description": "One real exact-name candidate matches every recorded service affiliation, corroborated by two venue/year events and two source pages. Other candidates may lack affiliations, but none has a competing organization match.",
    },
    "resolved_reviewed_profile": {
        "label": "Reviewed service identity",
        "description": "Primary sources confirm the fuller DBLP identity for this service name and affiliation. The reviewed decision is reapplied only while its recorded evidence scope still matches.",
    },
    "resolved_reviewed_service_alias": {
        "label": "Reviewed service-name alias",
        "description": "Institutional evidence links this service-name spelling to an existing matched researcher. The service history is attached to that identity without merging another profile's publications.",
    },
    "resolved_profile_affiliation": {
        "label": "Fuller name + corroborated affiliation",
        "description": "A compatible fuller-name profile replaces a sparse exact-name profile after complete organization matching on separate service pages. Other candidates have no competing organization match.",
    },
}

PRIMARY_AFFILIATION_DECISION_RULES = {
    "automatic_publication_affiliation": {
        "label": "Latest available publisher affiliation",
        "description": "Author-specific publisher DOI metadata is checked automatically for all known publications in the researcher's latest publication year. Exact ORCID or unique full-name matching is required. The latest usable publication date wins only when partial dates, joint affiliations and dated service evidence do not leave an unresolved conflict.",
    },
    "verified_publication_affiliation": {
        "label": "Verified recent publication affiliation",
        "description": "A manually checked author-specific affiliation from a linked publication, at least as recent as the latest dated service affiliation, takes precedence over undated DBLP notes. Conflicting publication affiliations in the same newest year require review.",
    },
    "service_corroborates_dblp_current": {
        "label": "Current DBLP affiliation corroborated by recent service",
        "description": "The newest dated database-service affiliation matches one of DBLP's current affiliations directly or through the pinned ROR organization snapshot; the DBLP label is displayed.",
    },
    "single_dblp_current": {
        "label": "Single current DBLP affiliation",
        "description": "DBLP exposes exactly one current affiliation, so that affiliation is used as the primary display affiliation.",
    },
    "multiple_dblp_current_first": {
        "label": "First of multiple current DBLP affiliations",
        "description": "DBLP exposes multiple current affiliations and recent service evidence does not uniquely select one; the first DBLP affiliation in source order is used and the ambiguity is flagged.",
    },
    "dblp_current_over_stale_service": {
        "label": "DBLP fallback with conflicting service affiliation",
        "description": "The newest dated database-service affiliation does not match any DBLP-current affiliation. Without verified recent publication evidence, the DBLP affiliation remains the fallback and the disagreement is flagged; either source may be outdated.",
    },
    "latest_service_only": {
        "label": "Latest database-service affiliation",
        "description": "DBLP exposes no current affiliation, so the newest dated affiliation observed on the project's database-community service pages is used.",
    },
    "latest_service_only_conflict": {
        "label": "Latest database-service affiliation with same-year conflict",
        "description": "DBLP exposes no current affiliation and multiple different affiliations occur in the newest service year. The most frequently observed affiliation is used, with a deterministic first-observed tie-break; the conflict is flagged.",
    },
    "known_affiliation_fallback": {
        "label": "Known affiliation fallback",
        "description": "Neither a DBLP-current affiliation nor a dated service affiliation is available, so another affiliation already present in the project data is displayed as a low-confidence fallback.",
    },
    "no_affiliation": {
        "label": "No affiliation evidence",
        "description": "No usable affiliation evidence is available in the project's current data sources.",
    },
}

PUBLICATION_PERSON_MATCH_RULES = {
    "reviewed_publication_author": {
        "label": "Reviewed publication author",
        "description": "Source evidence confirms this specific paper belongs to the researcher despite a split short-name DBLP profile. The exception applies only to this paper.",
    },
    "dblp_author_pid": {
        "label": "Direct DBLP author PID",
        "description": "The publication record directly identifies the resolved researcher by DBLP person ID.",
    },
    "verified_orcid": {
        "label": "Verified ORCID",
        "description": "The publication author ORCID matches an ORCID stored on the resolved DBLP person record.",
    },
    "exact_dblp_author_signature": {
        "label": "Unique exact DBLP author signature",
        "description": "An exact case-sensitive DBLP author signature belongs uniquely to the resolved researcher in the target population.",
    },
}

PUBLICATION_ENTITY_RULES = {
    "single_record": {
        "label": "Single DBLP record",
        "description": "No publication-version merge was applied; this work is represented by one DBLP record.",
    },
    "dblp_rel_versionOf": {
        "label": "DBLP versionOf relation",
        "description": "DBLP explicitly links the records through a versionOf relation.",
    },
    "shared_doi": {
        "label": "Shared DOI in DBLP",
        "description": "The records share the same DOI in DBLP metadata.",
    },
    "exact_title_authors_preprint_published": {
        "label": "Exact title + exact author set",
        "description": "A CoRR preprint and exactly one archival destination share the exact normalized title and complete normalized author set within the conservative year window.",
    },
    "explicit_preprint_version_marker": {
        "label": "Explicit preprint version marker",
        "description": "A one-to-one preprint/archival pair differs only by the literal trailing '(Extended Version)' or '(full version)' marker and has the same complete author set.",
    },
    "arxiv_doi": {
        "label": "arXiv DOI → unique archival DBLP record",
        "description": "Exact-ID arXiv metadata supplies a DOI with one archival DBLP target; the target also passes the exact-current-title or mutual-majority-author safety guard.",
    },
    "arxiv_current_title_exact": {
        "label": "Exact current arXiv title + exact author set",
        "description": "The current arXiv title and complete DBLP author set identify exactly one archival DBLP record within the 0–2 year publication window.",
    },
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


def json_object_list(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        parsed = value
    else:
        try:
            parsed = json.loads(str(value))
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def unique(values: Iterable[Any]) -> list[str]:
    return sorted(
        {clean(value) for value in values if clean(value)}, key=lambda value: (value.casefold(), value)
    )


def unique_preserve_order(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean(value)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def choose_primary_affiliation_evidence(
    current_affiliations: list[tuple[str, str]],
    service_events: Iterable[dict[str, Any]],
    fallback_affiliations: Iterable[str],
    *,
    affiliation_matcher: Any | None = None,
) -> dict[str, Any]:
    """Choose a primary affiliation and expose the evidence behind the choice.

    Only project-local evidence participates in the decision: DBLP current
    affiliation notes, dated database-community service affiliations, and the
    pinned ROR snapshot for exact organization reconciliation. ROR is used only
    to decide whether two already-observed labels refer to the same organization;
    it never supplies a new displayed affiliation.
    """
    current = [
        (clean(raw), clean(display))
        for raw, display in current_affiliations
        if clean(raw) or clean(display)
    ]

    dated_events = [
        event
        for event in service_events
        if event.get("year") is not None
        and (
            event.get("displayAffiliation")
            or event.get("rawAffiliations")
            or ((event.get("provenance") or {}).get("affiliations") or [])
        )
    ]
    latest_year: int | None = None
    ordered_candidates: list[dict[str, Any]] = []
    if dated_events:
        latest_year = max(int(event["year"]) for event in dated_events)
        candidate_index: dict[tuple[str, str], int] = {}
        for event in dated_events:
            if int(event["year"]) != latest_year:
                continue
            candidates: list[tuple[str, str]] = []
            provenance_affiliations = list(
                ((event.get("provenance") or {}).get("affiliations") or [])
            )
            if provenance_affiliations:
                for affiliation in provenance_affiliations:
                    raw = clean(affiliation.get("raw"))
                    display = clean(affiliation.get("display"))
                    candidates.append((raw, display or clean_affiliation_label(raw)))
            else:
                raw_values = list(event.get("rawAffiliations") or [])
                display_values = list(event.get("displayAffiliations") or [])
                if raw_values:
                    for index, raw in enumerate(raw_values):
                        display = (
                            display_values[index]
                            if index < len(display_values)
                            else clean_affiliation_label(raw)
                        )
                        candidates.append((clean(raw), clean(display)))
                elif event.get("displayAffiliation"):
                    display = clean(event["displayAffiliation"])
                    candidates.append((display, display))

            source_ids = list((event.get("provenance") or {}).get("sourceIds") or [])
            for raw, display in candidates:
                if not display:
                    continue
                key = (raw.casefold(), display.casefold())
                if key not in candidate_index:
                    candidate_index[key] = len(ordered_candidates)
                    ordered_candidates.append(
                        {
                            "raw": raw,
                            "display": display,
                            "count": 0,
                            "sourceIds": [],
                        }
                    )
                item = ordered_candidates[candidate_index[key]]
                item["count"] += 1
                item["sourceIds"] = unique(
                    list(item["sourceIds"]) + source_ids
                )

        # A service-only fallback should be stable and should prefer repeated
        # evidence within the latest year. Equal-frequency candidates retain the
        # first-observed order from the project data.
        ordered_candidates = sorted(
            enumerate(ordered_candidates),
            key=lambda pair: (-int(pair[1]["count"]), pair[0]),
        )
        ordered_candidates = [item for _index, item in ordered_candidates]

    def current_matches(
        candidate: dict[str, Any],
    ) -> tuple[list[tuple[str, str]], str]:
        service_raw = clean(candidate.get("raw"))
        service_display = clean(candidate.get("display"))
        service_keys = {
            normalize_name(service_raw),
            normalize_name(service_display),
        } - {""}
        direct = []
        for current_raw, current_display in current:
            current_keys = {
                normalize_name(current_raw),
                normalize_name(current_display),
            } - {""}
            if service_keys & current_keys:
                direct.append((current_raw, current_display))
        if direct:
            return direct, "direct"

        if affiliation_matcher is None:
            return [], ""
        service_ror_ids = set(
            affiliation_matcher.match_affiliation(service_raw or service_display)
        )
        if not service_ror_ids:
            return [], ""
        matches = []
        for current_raw, current_display in current:
            current_ror_ids = set(
                affiliation_matcher.match_affiliation(current_raw or current_display)
            )
            if service_ror_ids & current_ror_ids:
                matches.append((current_raw, current_display))
        return matches, ("ror" if matches else "")

    latest_service_labels = unique_preserve_order(
        item["display"] for item in ordered_candidates
    )
    latest_service_source_ids = unique(
        source_id
        for item in ordered_candidates
        for source_id in item.get("sourceIds", [])
    )
    current_labels = unique_preserve_order(
        display or raw for raw, display in current
    )

    candidate_matches = [
        (candidate, *current_matches(candidate)) for candidate in ordered_candidates
    ]
    for candidate, matches, match_method in candidate_matches:
        if matches:
            # If an organization label maps to more than one DBLP-current entry,
            # preserve DBLP source order rather than inventing another ranking.
            matched_set = {(raw, display) for raw, display in matches}
            selected = next(
                (pair for pair in current if pair in matched_set),
                matches[0],
            )
            selected_label = selected[1] or selected[0]

            competing_labels: list[str] = []
            for other_candidate, other_matches, _other_match_method in candidate_matches:
                if other_candidate is candidate:
                    continue
                if not other_matches:
                    competing_labels.append(other_candidate["display"])
                    continue
                other_labels = {display or raw for raw, display in other_matches}
                if selected_label not in other_labels:
                    competing_labels.append(other_candidate["display"])

            ambiguous_match = len(matches) > 1
            service_disagreement = bool(competing_labels)
            notes: list[str] = []
            if ambiguous_match:
                notes.append(
                    "The service affiliation matches multiple DBLP-current labels; DBLP source order breaks the tie."
                )
            if service_disagreement:
                notes.append(
                    "Other affiliations are also observed in the newest service year; the DBLP-current affiliation corroborated by service evidence is selected and the disagreement is flagged."
                )
            return {
                "affiliation": selected_label,
                "decisionKey": "service_corroborates_dblp_current",
                "confidence": "medium" if (ambiguous_match or service_disagreement) else "high",
                "sourceIds": unique(["dblp_snapshot", *latest_service_source_ids]),
                "methodIds": [
                    "primary_affiliation_selection",
                    *(["ror_affiliation_reconciliation"] if match_method == "ror" else []),
                ],
                "currentAffiliations": current_labels,
                "latestServiceYear": latest_year,
                "latestServiceAffiliations": latest_service_labels,
                "matchedServiceAffiliation": candidate["display"],
                "hasConflict": ambiguous_match or service_disagreement,
                "notes": notes,
            }

    if current:
        service_conflict = bool(ordered_candidates)
        multiple_current = len(current) > 1
        if service_conflict:
            decision_key = "dblp_current_over_stale_service"
        elif multiple_current:
            decision_key = "multiple_dblp_current_first"
        else:
            decision_key = "single_dblp_current"
        notes: list[str] = []
        if service_conflict:
            notes.append(
                "Newest service affiliation does not match any DBLP-current affiliation; DBLP-current evidence is preferred."
            )
        if multiple_current:
            notes.append(
                "DBLP exposes multiple current affiliations and no recent service affiliation uniquely selects one; the first DBLP entry is displayed."
            )
        selected_label = current[0][1] or current[0][0]
        return {
            "affiliation": selected_label,
            "decisionKey": decision_key,
            "confidence": "medium" if (service_conflict or multiple_current) else "high",
            "sourceIds": unique(["dblp_snapshot", *latest_service_source_ids]),
            "methodIds": ["primary_affiliation_selection"],
            "currentAffiliations": current_labels,
            "latestServiceYear": latest_year,
            "latestServiceAffiliations": latest_service_labels,
            "matchedServiceAffiliation": "",
            "hasConflict": service_conflict or multiple_current,
            "notes": notes,
        }

    if ordered_candidates:
        selected = ordered_candidates[0]
        same_year_conflict = len(latest_service_labels) > 1
        return {
            "affiliation": selected["display"],
            "decisionKey": (
                "latest_service_only_conflict"
                if same_year_conflict
                else "latest_service_only"
            ),
            "confidence": "low" if same_year_conflict else "medium",
            "sourceIds": latest_service_source_ids,
            "methodIds": ["primary_affiliation_selection"],
            "currentAffiliations": [],
            "latestServiceYear": latest_year,
            "latestServiceAffiliations": latest_service_labels,
            "matchedServiceAffiliation": selected["display"],
            "hasConflict": same_year_conflict,
            "notes": (
                ["Multiple different affiliations occur in the newest service year; repeated observations are preferred, then first-observed order."]
                if same_year_conflict
                else ["DBLP has no current affiliation; the newest dated database-service affiliation is used."]
            ),
        }

    fallback = next((clean(value) for value in fallback_affiliations if clean(value)), "")
    if fallback:
        return {
            "affiliation": fallback,
            "decisionKey": "known_affiliation_fallback",
            "confidence": "low",
            "sourceIds": ["dblp_snapshot"],
            "methodIds": ["primary_affiliation_selection"],
            "currentAffiliations": [],
            "latestServiceYear": None,
            "latestServiceAffiliations": [],
            "matchedServiceAffiliation": "",
            "hasConflict": False,
            "notes": ["No current DBLP or dated service affiliation is available."],
        }

    return {
        "affiliation": "",
        "decisionKey": "no_affiliation",
        "confidence": "low",
        "sourceIds": [],
        "methodIds": ["primary_affiliation_selection"],
        "currentAffiliations": [],
        "latestServiceYear": None,
        "latestServiceAffiliations": [],
        "matchedServiceAffiliation": "",
        "hasConflict": False,
        "notes": ["No usable affiliation evidence is available in the project data."],
    }


def choose_primary_affiliation(
    current_affiliations: list[tuple[str, str]],
    service_events: Iterable[dict[str, Any]],
    fallback_affiliations: Iterable[str],
    *,
    affiliation_matcher: Any | None = None,
) -> str:
    """Backward-compatible string wrapper around the evidence-aware selector."""
    return choose_primary_affiliation_evidence(
        current_affiliations,
        service_events,
        fallback_affiliations,
        affiliation_matcher=affiliation_matcher,
    )["affiliation"]


def service_source_id(url: str) -> str:
    normalized = clean(url)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"service_page:{digest}"


def arxiv_source_id(arxiv_id: str) -> str:
    normalized = clean(arxiv_id)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"arxiv_record:{digest}"


def service_method_id(source_db: str, venue: str) -> str:
    return SERVICE_PARSER_METHODS.get(
        clean(source_db), VENUE_PARSER_METHODS.get(clean(venue).upper(), "service_structural_parser")
    )


def local_meta_value(conn: sqlite3.Connection, key: str) -> str:
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "local_index_meta" not in tables:
        return ""
    row = conn.execute(
        "SELECT value FROM local_index_meta WHERE key=?", (key,)
    ).fetchone()
    return clean(row[0]) if row else ""


def dblp_snapshot_artifact_url(snapshot_id: str) -> str:
    match = re.fullmatch(r"dblp-(\d{4})-\d{2}-\d{2}", clean(snapshot_id))
    if not match:
        return "https://dblp.org/xml/"
    return (
        "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/"
        f"{match.group(1)}/{snapshot_id}.xml.gz"
    )


def build_provenance_catalog(snapshot_id: str) -> dict[str, Any]:
    snapshot_label = clean(snapshot_id) or "pinned local snapshot"
    return {
        "schemaVersion": PROVENANCE_SCHEMA_VERSION,
        "sources": {
            "crossref_affiliations": {
                "kind": "supplementary_metadata",
                "label": "Crossref publisher metadata", "provider": "Crossref",
                "url": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/",
                "role": "Author affiliations from the latest available publication metadata.",
            },
            "dblp_snapshot": {
                "kind": "bibliographic_snapshot",
                "label": "DBLP monthly XML snapshot",
                "provider": "DBLP",
                "release": snapshot_label,
                "url": dblp_snapshot_artifact_url(snapshot_label),
                "role": "Names, researcher identities, and publication records used throughout the website.",
            },
            "arxiv_metadata_cache": {
                "kind": "supplementary_metadata",
                "label": "arXiv exact-ID metadata",
                "provider": "arXiv",
                "url": "https://export.arxiv.org/api/query",
                "role": "Used only to confirm selected publication-version matches when DBLP alone is ambiguous.",
            },
            "ror_snapshot": {
                "kind": "organization_snapshot",
                "label": "ROR organization snapshot",
                "provider": "Research Organization Registry",
                "release": ROR_RELEASE,
                "url": "https://ror.org/registry/",
                "role": "Used as exact organization evidence for researcher identity resolution and for reconciling already-observed DBLP/service affiliation labels; it never supplies a new display affiliation.",
            },
        },
        "methods": {
            "scrape_cidr_reviewers": {
                "label": "CIDR structural service-page parser",
                "technical": "scripts/scrape_cidr_reviewers.py",
                "description": "Parses official CIDR officer and research-program service structures into normalized person/role observations.",
            },
            "scrape_edbt_reviewers": {
                "label": "EDBT structural service-page parser",
                "technical": "scripts/scrape_edbt_reviewers.py",
                "description": "Parses official EDBT committee, officer, industrial, and demonstration service structures into normalized observations.",
            },
            "scrape_icde_reviewers": {
                "label": "ICDE structural service-page parser",
                "technical": "scripts/scrape_icde_reviewers.py",
                "description": "Parses official ICDE research, officer, industrial, and demonstration service structures with year-specific structural handling.",
            },
            "scrape_sigmod_reviewers": {
                "label": "SIGMOD/PODS structural service-page parser",
                "technical": "scripts/scrape_sigmod_reviewers.py",
                "description": "Parses official SIGMOD research, officer, industrial, demonstration, and co-located PODS service pages.",
            },
            "scrape_vldb_reviewers": {
                "label": "VLDB structural service-page parser",
                "technical": "scripts/scrape_vldb_reviewers.py",
                "description": "Parses official PVLDB review-board and VLDB officer pages plus industry and demonstration committees in the annual PVLDB front matter.",
            },
            "service_structural_parser": {
                "label": "Venue-specific structural service parser",
                "description": "Extracts person, role, year, affiliation, and source-page evidence from an official service page.",
            },
            "person_entity_resolution": {
                "label": "Researcher identity matching",
                "technical": "scripts/enrich_reviewers_with_dblp.py and guarded follow-up resolvers",
                "description": "Links names observed on official service pages to DBLP researcher identities using conservative, auditable rules.",
            },
            "ror_identity_evidence": {
                "label": "Exact ROR organization evidence",
                "technical": "scripts/ror_affiliations.py",
                "description": "Canonicalizes explicit organization names/aliases from the pinned ROR snapshot for identity evidence only; no fuzzy organization matching.",
            },
            "dblp_publication_scan": {
                "label": "DBLP publication collection",
                "technical": "scripts/scan_dblp_publications.py",
                "description": "Collects publication records from the pinned DBLP snapshot for researchers already matched to DBLP.",
            },
            "publication_person_linkage": {
                "label": "Strict publication → person attachment",
                "description": "Attaches a DBLP publication to a resolved researcher only by direct DBLP PID, verified ORCID, or an exact uniquely-owned DBLP author signature.",
            },
            "publication_entity_resolution": {
                "label": "Publication version linking",
                "technical": "scripts/resolve_publication_entities.py",
                "description": "Links only high-confidence versions of the same work; selected arXiv metadata is used only as supporting evidence.",
            },
            "publication_classification": {
                "label": "Publication classification",
                "technical": "scripts/build_site_data.py",
                "description": "Classifies contextual works using explicit workshop metadata and a reviewed venue-to-category CSV with dated rules, evidence URLs, and reasons, then structural DBLP record types. Main ML membership and CoRR recognition are protected from display mappings.",
            },
            "affiliation_display_cleaning": {
                "label": "Display-only affiliation cleanup",
                "technical": "scripts/display_cleaning.py",
                "description": "Normalizes affiliation spelling for display while preserving every raw affiliation string and the cleanup reason in provenance/audit output.",
            },
            "primary_affiliation_selection": {
                "label": "Primary affiliation selection",
                "technical": "scripts/build_site_data.py",
                "description": "Automatically uses the latest usable author-specific publisher affiliation with exact identity and date checks. Reviewed publication evidence supplements it; older reviews cannot overwrite newer automatic evidence. DBLP/service labels remain fallbacks when evidence is missing or conflicting.",
            },
            "ror_affiliation_reconciliation": {
                "label": "Exact ROR affiliation reconciliation",
                "technical": "scripts/ror_affiliations.py",
                "description": "Uses the pinned ROR snapshot to test whether a DBLP affiliation label and a service-page affiliation label refer to the same organization. ROR does not provide the displayed affiliation itself.",
            },
            "derived_metrics": {
                "label": "Website statistics",
                "technical": "scripts/build_site_data.py and website/deploy/assets/app.js",
                "description": "Calculates person and publication counts from matched researchers, qualifying database-community service, and main ML papers.",
            },
        },
        "decisionRules": {
            "identity": IDENTITY_DECISION_RULES,
            "publicationPersonMatch": PUBLICATION_PERSON_MATCH_RULES,
            "publicationEntity": PUBLICATION_ENTITY_RULES,
            "primaryAffiliation": PRIMARY_AFFILIATION_DECISION_RULES,
        },
        "recordTypes": {
            "publication": {
                "baseSourceIds": ["dblp_snapshot"],
                "arxivSourceId": "arxiv_metadata_cache",
                "arxivResolutionPrefix": "arxiv_",
                "sourceField": "provenance.sourceIds",
                "externalEvidenceField": "arxivIds",
                "extractionMethodIds": ["dblp_publication_scan"],
                "identityMethodIds": ["publication_person_linkage"],
                "resolutionMethodIds": ["publication_entity_resolution"],
                "classificationMethodIds": ["publication_classification"],
                "decisionFields": {
                    "personMatch": "matchBasis",
                    "workResolution": "publicationEntityBasis",
                    "canonicalRecord": "canonicalDblpKey",
                    "versionRecords": "versionDblpKeys",
                },
            },
            "serviceEvent": {
                "sourceField": "provenance.sourceIds",
                "extractionMethodField": "provenance.extractionMethodIds",
                "identityMethodIds": ["person_entity_resolution"],
            },
            "researcherIdentity": {
                "sourceField": "provenance.identity.sourceIds",
                "decisionField": "provenance.identity.decisionKeys",
            },
            "affiliation": {
                "rawValueField": "provenance.affiliations[].raw",
                "displayValueField": "provenance.affiliations[].display",
                "methodIds": ["affiliation_display_cleaning"],
            },
            "primaryAffiliation": {
                "valueField": "primaryAffiliation",
                "provenanceField": "provenance.primaryAffiliation",
                "decisionField": "provenance.primaryAffiliation.decisionKey",
            },
        },
        "metrics": {
            "db_ml_researchers": {
                "label": "Crossover researchers",
                "description": "Distinct matched researchers with qualifying database-community service and at least one main NeurIPS, ICML, or ICLR paper.",
                "methodIds": ["derived_metrics"],
                "sourceKinds": ["official_service_page", "bibliographic_snapshot"],
            },
            "core_ml_papers": {
                "label": "Main ML papers by database researchers",
                "description": "Distinct main-conference NeurIPS/NIPS, ICML, or ICLR papers authored by matched database-community researchers.",
                "methodIds": ["publication_entity_resolution", "publication_classification", "derived_metrics"],
                "sourceKinds": ["bibliographic_snapshot", "supplementary_metadata"],
            },
            "venue_core_ml_researchers": {
                "label": "Database researchers with papers at an ML venue",
                "description": "Distinct matched researchers with at least one main paper at the named ML venue.",
                "methodIds": ["derived_metrics"],
                "sourceKinds": ["official_service_page", "bibliographic_snapshot"],
            },
            "crossover_matrix": {
                "label": "Database venue × ML venue matrix",
                "description": "Each cell shows the share of researchers who served at the database venue and also have at least one main paper at the ML venue. Click a cell to filter the Research explorer; click the selected cell again to clear it.",
                "methodIds": ["derived_metrics"],
                "sourceKinds": ["official_service_page", "bibliographic_snapshot"],
            },
            "publication_activity": {
                "label": "Publication activity",
                "description": "Shows how many main NeurIPS, ICML, and ICLR papers the matched researchers published in each year. Click a bar to inspect the papers.",
                "methodIds": ["publication_entity_resolution", "publication_classification", "derived_metrics"],
                "sourceKinds": ["bibliographic_snapshot", "supplementary_metadata"],
            },
        },
    }


def register_service_source(
    catalog: dict[str, Any], *, url: str, venue: str, year: int | None
) -> str:
    normalized = clean(url)
    if not normalized:
        return ""
    source_id = service_source_id(normalized)
    catalog["sources"].setdefault(
        source_id,
        {
            "kind": "official_service_page",
            "label": f"{clean(venue) or 'Database service'} {year or ''} official service page".strip(),
            "provider": clean(venue) or "Database conference",
            "year": year,
            "url": normalized,
            "role": "Official page used as evidence for database-community service.",
        },
    )
    return source_id


def register_arxiv_source(catalog: dict[str, Any], arxiv_id: str) -> str:
    normalized = clean(arxiv_id)
    if not normalized:
        return ""
    source_id = arxiv_source_id(normalized)
    catalog["sources"].setdefault(
        source_id,
        {
            "kind": "supplementary_metadata",
            "label": f"arXiv {normalized}",
            "provider": "arXiv",
            "recordId": normalized,
            "url": f"https://arxiv.org/abs/{normalized}",
            "role": "Exact external record used as supplementary publication-version evidence",
        },
    )
    return source_id


def artifact_fingerprint(path: Path) -> dict[str, Any]:
    """Return a portable content fingerprint without exposing local paths."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {
        "filename": path.name,
        "sizeBytes": stat.st_size,
        "sha256": digest.hexdigest(),
    }


def combined_artifact_fingerprint(paths: dict[str, Path]) -> str:
    """Return one deterministic SHA-256 over the content hashes of build inputs."""
    digest = hashlib.sha256()
    for key, path in sorted(paths.items()):
        artifact = artifact_fingerprint(path)
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(artifact["sha256"].encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def latest_reviewer_fetch_at(data_dir: Path) -> str:
    """Return the newest source fetch timestamp recorded by reviewer scrapers."""
    newest = ""
    for filename in REVIEWER_DATABASES:
        path = data_dir / filename
        if not path.exists():
            continue
        conn = sqlite3.connect(path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "reviewer_records" not in tables:
                continue
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(reviewer_records)")
            }
            if "fetched_at" not in columns:
                continue
            row = conn.execute("SELECT MAX(fetched_at) FROM reviewer_records").fetchone()
            value = clean(row[0] if row else "")
            if value and value > newest:
                newest = value
        finally:
            conn.close()
    return newest


def arxiv_cache_refreshed_at(path: Path) -> str:
    """Return when the arXiv metadata cache was last refreshed."""
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        value = clean(payload.get("refreshedAt"))
        if value:
            return value
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        return ""


def publication_provenance(
    catalog: dict[str, Any], paper: dict[str, Any]
) -> dict[str, Any]:
    source_ids = ["dblp_snapshot"]
    bases = [
        clean(value)
        for value in clean(paper.get("publicationEntityBasis")).split(";")
        if clean(value)
    ]
    if any(basis.startswith("arxiv_") for basis in bases):
        exact_arxiv_sources = unique(
            register_arxiv_source(catalog, arxiv_id)
            for arxiv_id in paper.get("arxivIds", [])
        )
        source_ids.extend(exact_arxiv_sources or ["arxiv_metadata_cache"])

    return {
        "sourceIds": unique(source_ids),
        "extractionMethodIds": ["dblp_publication_scan"],
        "identityMethodIds": ["publication_person_linkage"],
        "resolutionMethodIds": ["publication_entity_resolution"],
        "classificationMethodIds": ["publication_classification"],
    }


def require_tables(
    conn: sqlite3.Connection, names: Iterable[str], label: str
) -> None:
    have = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = [name for name in names if name not in have]
    if missing:
        raise RuntimeError(f"{label} is missing table(s): {', '.join(missing)}")


def publication_scan_matches_current_targets(
    data_dir: Path, entity_db: Path, raw_csv: Path | None, totals_csv: Path | None
) -> bool:
    """Verify a cached scan after service-only edits leave its targets unchanged."""
    if raw_csv is None or totals_csv is None:
        return False
    try:
        try:
            from .scan_dblp_publications import (
                SNAPSHOT_FILENAME, load_targets, scan_cache_is_current,
            )
        except ImportError:
            from scan_dblp_publications import (
                SNAPSHOT_FILENAME, load_targets, scan_cache_is_current,
            )
        snapshot = data_dir / "dblp_snapshot" / SNAPSHOT_FILENAME
        if not snapshot.exists():
            return False
        return scan_cache_is_current(raw_csv, totals_csv, snapshot, load_targets(entity_db))
    except (OSError, sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError):
        return False


def assert_pipeline_freshness(
    data_dir: Path,
    *,
    entity_db: Path,
    publications_csv: Path,
    raw_publications_csv: Path | None = None,
    main_totals_csv: Path | None = None,
    arxiv_cache: Path | None = None,
    allow_stale: bool = False,
) -> None:
    """Refuse to publish a site dataset built from stale upstream artifacts."""
    if allow_stale:
        return

    er_mtime = entity_db.stat().st_mtime
    newer_reviewers = [
        data_dir / filename
        for filename in REVIEWER_DATABASES
        if (data_dir / filename).exists()
        and (data_dir / filename).stat().st_mtime > er_mtime
    ]
    if newer_reviewers:
        names = ", ".join(path.name for path in newer_reviewers)
        raise RuntimeError(
            "Entity resolution is older than reviewer-service input(s): "
            f"{names}. Rerun scripts/enrich_reviewers_with_dblp.py before building the site "
            "(or pass --allow-stale-inputs only for an intentional diagnostic build)."
        )

    scan_older = any(
        path is not None and path.exists() and er_mtime > path.stat().st_mtime
        for path in (raw_publications_csv, main_totals_csv)
    )
    verified_cached_scan = scan_older and publication_scan_matches_current_targets(
        data_dir, entity_db, raw_publications_csv, main_totals_csv
    )

    if raw_publications_csv is not None and raw_publications_csv.exists():
        if er_mtime > raw_publications_csv.stat().st_mtime and not verified_cached_scan:
            raise RuntimeError(
                "The DBLP publication scan is older than entity_resolution.sqlite. "
                "Rerun scripts/scan_dblp_publications.py before building the site "
                "(or pass --allow-stale-inputs only for an intentional diagnostic build)."
            )
        if raw_publications_csv.stat().st_mtime > publications_csv.stat().st_mtime:
            raise RuntimeError(
                "Publication entity resolution is older than the DBLP publication scan. "
                "Rerun scripts/resolve_publication_entities.py before building the site "
                "(or pass --allow-stale-inputs only for an intentional diagnostic build)."
            )
    elif entity_db.stat().st_mtime > publications_csv.stat().st_mtime:
        raise RuntimeError(
            "The DBLP publication scan is older than entity_resolution.sqlite. "
            "Rerun scripts/scan_dblp_publications.py before building the site "
            "(or pass --allow-stale-inputs only for an intentional diagnostic build)."
        )

    if arxiv_cache is not None and arxiv_cache.exists():
        if arxiv_cache.stat().st_mtime > publications_csv.stat().st_mtime:
            raise RuntimeError(
                "Publication entity resolution is older than the arXiv evidence cache. "
                "Rerun scripts/resolve_publication_entities.py before building the site "
                "(or pass --allow-stale-inputs only for an intentional diagnostic build)."
            )

    if main_totals_csv is not None:
        if not main_totals_csv.exists():
            raise RuntimeError(
                f"Missing {main_totals_csv.name}. Rerun scripts/scan_dblp_publications.py "
                "to generate conference-size normalization totals."
            )
        if er_mtime > main_totals_csv.stat().st_mtime and not verified_cached_scan:
            raise RuntimeError(
                "The main-ML conference-size totals are older than entity_resolution.sqlite. "
                "Rerun scripts/scan_dblp_publications.py before building the site "
                "(or pass --allow-stale-inputs only for an intentional diagnostic build)."
            )


def coverage(values: dict[str, set[int]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for venue, years in sorted(values.items()):
        if years:
            result[venue] = {"min": min(years), "max": max(years)}
    return result


def load_main_venue_year_totals(path: Path) -> dict[str, dict[str, int]]:
    if not path.exists():
        raise RuntimeError(
            f"Missing {path.name}. Run scripts/scan_dblp_publications.py first."
        )

    totals: dict[str, dict[str, int]] = defaultdict(dict)
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            venue = clean(row.get("venue"))
            if venue not in {"NeurIPS", "ICML", "ICLR"}:
                continue
            try:
                year = int(row.get("year") or "")
                count = int(row.get("paper_count") or "")
            except ValueError:
                continue
            if count < 0:
                continue
            totals[venue][str(year)] = count
    return {
        venue: dict(sorted(years.items(), key=lambda item: int(item[0])))
        for venue, years in sorted(totals.items())
    }


def choose_name(
    names: Iterable[str],
    fallback: str,
    *,
    preferred_names: Iterable[str] = (),
) -> str:
    values = sorted(
        {clean(value) for value in names if clean(value)},
        key=lambda value: (value.casefold(), value),
    )
    if not values:
        return fallback

    detail = lambda value: (len(normalize_name(value).split()), len(value))
    best_detail = max(detail(value) for value in values)
    finalists = [value for value in values if detail(value) == best_detail]
    preferred = {clean(value) for value in preferred_names if clean(value)}
    preferred_finalists = [value for value in finalists if value in preferred]
    return (preferred_finalists or finalists)[0]


def normalized_metadata(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text.casefold()).strip()


def classify_publication(
    *,
    scope: str,
    dblp_key: str,
    year: int | None,
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
    - explicit non-ML workshop wording wins over a broad DB/conference bucket;
    - the readable CSV supplies explicit venue and database-namespace mappings;
    - remaining articles and proceedings use structural DBLP record types.
    """

    if scope == "main":
        return "main_ml", clean(main_venue) or clean(venue_label) or "Core ML"


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

    mapping = matching_category_rule(
        dblp_key=key, booktitle=booktitle, journal=journal, venue_label=venue_label, year=str(year or "")
    )
    # Specific reviewed overrides precede text heuristics. Broad namespaces do
    # not swallow explicit workshop labels inside a parent conference.
    if mapping and mapping.match_type == "exact":
        return mapping.category, mapping.venue_label or booktitle or journal or venue_label
    if re.search(r"\bworkshops?\b", metadata):
        return "workshop", booktitle or venue_label or journal or "Workshop"
    if mapping:
        return mapping.category, mapping.venue_label or booktitle or journal or venue_label

    if record_type == "article" or journal:
        return "journal", journal or venue_label or "Journal"

    if record_type == "inproceedings":
        return "other_conference", booktitle or venue_label or "Conference"

    return (
        "other",
        venue_label or booktitle or journal or "Other DBLP publication",
    )


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

    resolution_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(name_resolution)")
    }
    observation_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(reviewer_observations)")
    }
    best_orcid_source_select = (
        "r.best_orcid_source" if "best_orcid_source" in resolution_columns else "''"
    )
    notes_select = "r.notes" if "notes" in resolution_columns else "''"
    source_db_select = (
        "o.source_db" if "source_db" in observation_columns else "''"
    )

    query = f"""
        SELECT r.name_key, r.resolved_dblp_pid, r.resolved_dblp_name, r.best_orcid,
               {best_orcid_source_select}, r.representative_name, r.status,
               {notes_select}, o.name, o.venue, o.year, o.role,
               o.review_round, o.review_cycle, o.affiliation, o.source_url,
               {source_db_select}
        FROM name_resolution r
        JOIN reviewer_observations o ON o.name_key=r.name_key
        WHERE r.resolved_dblp_pid IS NOT NULL
        ORDER BY r.resolved_dblp_pid, o.year, o.venue
    """
    for row in conn.execute(query):
        (
            name_key,
            pid,
            resolved_name,
            best_orcid,
            best_orcid_source,
            representative,
            resolution_status,
            resolution_notes,
            observed_name,
            venue,
            year,
            role,
            review_round,
            review_cycle,
            affiliation,
            source_url,
            source_db,
        ) = row
        pid = clean(pid)
        if not pid:
            continue
        item = people.setdefault(
            pid,
            {
                "pid": pid,
                "names": set(),
                "preferred_names": set(),
                "aliases": set(),
                "affiliations": set(),
                "current_affiliations": set(),
                "current_affiliations_ordered": [],
                "former_affiliations": set(),
                "venues": set(),
                "years": set(),
                "roles": set(),
                "orcids": set(),
                "identity_name_keys": set(),
                "identity_statuses": set(),
                "identity_notes": set(),
                "identity_best_orcid_sources": set(),
                "affiliation_evidence": defaultdict(
                    lambda: {
                        "service_urls": set(),
                        "source_dbs": set(),
                        "dblp": False,
                    }
                ),
                "events": defaultdict(
                    lambda: {
                        "rounds": set(),
                        "cycles": set(),
                        "affiliations": set(),
                        "sources": set(),
                        "source_dbs": set(),
                        "observations": 0,
                    }
                ),
            },
        )
        if clean(name_key):
            item["identity_name_keys"].add(clean(name_key))
        if clean(resolution_status):
            item["identity_statuses"].add(clean(resolution_status))
        if clean(resolution_notes):
            item["identity_notes"].add(clean(resolution_notes))
        if clean(best_orcid_source):
            item["identity_best_orcid_sources"].add(clean(best_orcid_source))
        if clean(resolved_name):
            item["names"].add(clean(resolved_name))
            item["preferred_names"].add(clean(resolved_name))
        if clean(representative) and clean(resolution_status) not in {
            "resolved_existing_pid_alias",
            "resolved_existing_pid_ror_alias",
        }:
            item["names"].add(clean(representative))
        if clean(observed_name):
            item["aliases"].add(clean(observed_name))
        if clean(affiliation):
            affiliation_value = clean(affiliation)
            item["affiliations"].add(affiliation_value)
            evidence = item["affiliation_evidence"][affiliation_value]
            if clean(source_url):
                evidence["service_urls"].add(clean(source_url))
            if clean(source_db):
                evidence["source_dbs"].add(clean(source_db))
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

        event_key = (
            clean(venue),
            int(year) if year is not None else None,
            clean(role),
        )
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
        if clean(source_db):
            event["source_dbs"].add(clean(source_db))

    dblp_people_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(dblp_people)")
    }
    have_affiliation_status = {
        "current_affiliations_json",
        "former_affiliations_json",
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
        pid,
        canonical,
        aliases_json,
        affiliations_json,
        current_affiliations_json,
        former_affiliations_json,
        orcids_json,
    ) in conn.execute(query):
        pid = clean(pid)
        if pid not in people:
            continue
        item = people[pid]
        if clean(canonical):
            item["names"].add(clean(canonical))
            item["preferred_names"].add(clean(canonical))
        item["aliases"].update(json_list(aliases_json))
        dblp_affiliations = json_list(affiliations_json)
        current_affiliations = json_list(current_affiliations_json)
        former_affiliations = json_list(former_affiliations_json)
        item["affiliations"].update(dblp_affiliations)
        item["current_affiliations"].update(current_affiliations)
        item["current_affiliations_ordered"] = unique_preserve_order(
            list(item["current_affiliations_ordered"]) + current_affiliations
        )
        item["former_affiliations"].update(former_affiliations)
        for affiliation in set(
            dblp_affiliations + current_affiliations + former_affiliations
        ):
            item["affiliation_evidence"][affiliation]["dblp"] = True
        item["orcids"].update(json_list(orcids_json))

    return people, venue_coverage


def load_publications(
    path: Path,
    known_pids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, set[int]], set[str]]:
    if not path.exists():
        raise RuntimeError(
            f"Missing {path.name}. Run scripts/scan_dblp_publications.py first."
        )

    by_pid: dict[str, dict[str, Any]] = {}
    main_coverage: dict[str, set[int]] = defaultdict(set)
    snapshot_ids: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = clean(row.get("dblp_pid"))
            if not pid or pid not in known_pids:
                continue
            if publication_row_issues(row):
                continue
            year = int(row["year"])
            scope = clean(row.get("scope")).casefold()
            scope = "main" if scope == "main" else "side"
            title = clean(row.get("title"))
            key = clean(row.get("dblp_key"))
            if not title or not key:
                continue
            work_id = clean(row.get("work_id")) or key
            canonical_key = clean(row.get("canonical_dblp_key")) or key
            version_keys = json_list(row.get("version_dblp_keys")) or [key]
            resolved_versions = json_object_list(row.get("publication_versions_json"))
            person_version_keys = (
                json_list(row["person_version_dblp_keys"])
                if clean(row.get("person_version_dblp_keys")) else None
            )
            publication_entity_basis = (
                clean(row.get("publication_entity_basis")) or "single_record"
            )
            arxiv_ids: list[str] = []
            if any(
                basis.startswith("arxiv_")
                for basis in publication_entity_basis.split(";")
            ):
                arxiv_ids = unique(
                    arxiv_id_from_publication_values(dblp_key=version_key)
                    for version_key in version_keys
                )
            try:
                version_count = int(row.get("version_count") or len(version_keys))
            except ValueError:
                version_count = len(version_keys)
            try:
                person_version_count = int(row.get("person_version_count") or "1")
            except ValueError:
                person_version_count = 1
            snapshot_id = clean(row.get("snapshot_id"))
            if snapshot_id:
                snapshot_ids.add(snapshot_id)

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
            venue_label = (
                clean(row.get("venue_label"))
                or venue
                or "Other DBLP publication"
            )
            record_type = clean(row.get("record_type"))
            booktitle = clean(row.get("booktitle"))
            journal = clean(row.get("journal"))
            publication_class, exact_venue = classify_publication(
                scope=scope,
                dblp_key=key,
                year=year,
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
            try:
                author_count = int(row.get("author_count") or "")
            except ValueError:
                author_count = None
            if author_count is not None and author_count < 1:
                author_count = None

            paper = {
                "title": title,
                "scope": scope,
                "venue": venue,
                "venueLabel": venue_label,
                "publicationClass": publication_class,
                "publicationClassLabel": PUBLICATION_CLASS_LABELS[
                    publication_class
                ],
                "exactVenue": exact_venue,
                "year": year,
                "url": clean(row.get("url")),
                "dblpKey": key,
                "workId": work_id,
                "canonicalDblpKey": canonical_key,
                "versionCount": max(version_count, 1),
                "versionDblpKeys": version_keys,
                "resolvedVersions": resolved_versions,
                **({"personVersionDblpKeys": person_version_keys}
                   if person_version_keys is not None else {}),
                "personVersionCount": max(person_version_count, 1),
                "publicationEntityBasis": publication_entity_basis,
                "arxivIds": arxiv_ids,
                "authorPosition": author_position,
                "authorCount": author_count,
                "localAuthorName": clean(row.get("author_signature")),
                "recordType": record_type,
                "publtype": clean(row.get("publtype")),
                "booktitle": booktitle,
                "journal": journal,
                "source": "dblp_snapshot",
                "matchBasis": clean(row.get("match_basis")),
            }
            if scope != "main" and publication_class != "preprint":
                mapping = matching_category_rule(
                    dblp_key=key, booktitle=booktitle, journal=journal,
                    venue_label=venue_label, year=str(year),
                )
                if mapping and mapping.category == publication_class:
                    paper["categoryMapping"] = {
                        "ruleId": mapping.rule_id,
                        "reason": mapping.reason,
                        "evidenceUrl": mapping.evidence_url,
                    }
            item["papers"][work_id] = paper
            if scope == "main" and venue:
                main_coverage[venue].add(year)

    return by_pid, main_coverage, snapshot_ids


def load_publication_affiliation_evidence(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read reviewed author-specific evidence; never infer it from coauthors."""
    if not path.exists():
        return {}
    required = {
        "dblp_pid", "person_name", "dblp_key", "publication_year",
        "affiliation", "source_url", "verified_on", "reason",
    }
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"{path.name}: missing publication-affiliation columns")
        for line, row in enumerate(reader, 2):
            values = {key: clean(row.get(key)) for key in required}
            try:
                year = int(values["publication_year"])
                verified = date.fromisoformat(values["verified_on"])
                url = urlparse(values["source_url"])
                if not all(values.values()) or not 1900 <= year <= verified.year:
                    raise ValueError("missing value or invalid publication year")
                if url.scheme != "https" or not url.netloc:
                    raise ValueError("source must be an HTTPS publication URL")
            except ValueError as exc:
                raise ValueError(f"{path.name}:{line}: {exc}") from exc
            evidence[values["dblp_pid"]].append({
                "personName": values["person_name"],
                "dblpKey": values["dblp_key"],
                "year": year,
                "affiliation": clean_affiliation_label(values["affiliation"]),
                "sourceUrl": values["source_url"],
                "verifiedOn": verified.isoformat(),
                "reason": values["reason"],
            })
    return dict(evidence)


def apply_publication_affiliation_evidence(
    primary: dict[str, Any],
    evidence: list[dict[str, Any]],
    papers: list[dict[str, Any]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Override an undated note only with reviewed evidence for this person's work.

    Older publications cannot override newer service evidence. Different
    affiliations in the newest reviewed publication year remain ambiguous.
    """
    if not evidence:
        return primary
    indexed_years: dict[str, int] = {}
    for paper in papers:
        owned = set(paper.get("personVersionDblpKeys", [paper.get("dblpKey")]))
        if paper.get("dblpKey") in owned:
            indexed_years[paper["dblpKey"]] = paper.get("year")
        for version in paper.get("resolvedVersions") or []:
            if version.get("dblpKey") in owned:
                indexed_years[version["dblpKey"]] = version.get("year")
    for item in evidence:
        if indexed_years.get(item["dblpKey"]) != item["year"]:
            raise ValueError(f"Affiliation evidence is not a matching publication/year for {item['personName']}: {item['dblpKey']}")
    newest_year = max(item["year"] for item in evidence)
    reviewed = [item for item in evidence if item["year"] == newest_year]
    result = {**primary, "publicationAffiliationEvidence": evidence}
    if newest_year < (primary.get("automaticAffiliationYear") or 0):
        result["notes"] = [*primary.get("notes", []), "Older manual evidence does not override newer automatic publisher evidence."]
        return result
    if newest_year < (primary.get("latestServiceYear") or 0):
        result["notes"] = [*primary.get("notes", []), "Reviewed publication affiliation predates the latest service evidence; display affiliation unchanged."]
        return result
    affiliations = unique(item["affiliation"] for item in reviewed)
    if len(affiliations) != 1:
        result.update({
            "hasConflict": True, "confidence": "low",
            "notes": [*primary.get("notes", []), "Newest reviewed publications list different affiliations; display affiliation unchanged pending review."],
        })
        return result
    selected = affiliations[0]
    disagreement = bool(primary.get("hasConflict") or (
        primary.get("affiliation") and primary["affiliation"] != selected
    ))
    source_ids = []
    for item in reviewed:
        digest = hashlib.sha256(f"{item['dblpKey']}|{item['personName']}|{item['affiliation']}".encode()).hexdigest()[:12]
        source_id = f"publication_affiliation:{digest}"
        provenance["sources"][source_id] = {
            "kind": "publication_affiliation", "provider": "Publication author information",
            "label": f"Publication affiliation ({newest_year})", "url": item["sourceUrl"],
            "publicationDblpKey": item["dblpKey"], "personName": item["personName"],
            "affiliation": selected, "verifiedOn": item["verifiedOn"],
        }
        source_ids.append(source_id)
    result.update({
        "affiliation": selected,
        "decisionKey": "verified_publication_affiliation",
        "confidence": "medium" if disagreement else "high",
        "hasConflict": disagreement,
        "sourceIds": unique([*primary.get("sourceIds", []), *source_ids]),
        "publicationAffiliationYear": newest_year,
        "notes": [f"Author-specific affiliation verified in a {newest_year} publication; undated DBLP notes do not establish recency."],
    })
    return result


def build_payload(
    db_conn: sqlite3.Connection,
    publications_csv: Path,
    *,
    affiliation_matcher: Any | None = None,
    publication_affiliations_path: Path | None = None,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    db, db_coverage = load_db_people(db_conn)
    publications, main_coverage, publication_snapshot_ids = load_publications(
        publications_csv, set(db)
    )
    entity_snapshot_id = local_meta_value(db_conn, "snapshot_id")
    snapshot_id = (
        next(iter(publication_snapshot_ids))
        if len(publication_snapshot_ids) == 1
        else entity_snapshot_id
    )
    provenance = build_provenance_catalog(snapshot_id)
    publication_affiliations_path = publication_affiliations_path or publications_csv.parent / PUBLICATION_AFFILIATIONS_CSV
    publication_affiliations = load_publication_affiliation_evidence(publication_affiliations_path)
    automatic_path = publications_csv.parent / AUTOMATIC_AFFILIATIONS_JSON
    automatic_affiliations = {}
    if automatic_path.exists():
        automatic_snapshot = json.loads(automatic_path.read_text(encoding="utf-8"))
        if automatic_snapshot.get("schemaVersion") != AFFILIATION_POLICY_VERSION:
            raise ValueError("Automatic affiliation policy changed; rerun scripts/refresh_affiliations.py")
        expected_publications = artifact_fingerprint(publications_csv)
        if automatic_snapshot.get("inputs", {}).get("publications") != expected_publications:
            raise ValueError("Automatic affiliation evidence is older than the publication corpus; rerun scripts/refresh_affiliations.py")
        # A changed PID mapping must not silently inherit another person's cache.
        db_file = next((Path(row[2]) for row in db_conn.execute("PRAGMA database_list") if row[1] == "main" and row[2]), None)
        if db_file and automatic_snapshot.get("inputs", {}).get("entityResolution") != artifact_fingerprint(db_file):
            raise ValueError("Automatic affiliation evidence is older than researcher resolution; rerun scripts/refresh_affiliations.py")
        automatic_affiliations = automatic_snapshot.get("people", {})
        crossref_refreshed_at = evidence_refreshed_at(automatic_snapshot)
        if crossref_refreshed_at:
            provenance["sources"]["crossref_affiliations"]["refreshedAt"] = crossref_refreshed_at
    unknown_pids = set(publication_affiliations) - set(db)
    if unknown_pids:
        raise ValueError(f"Unknown DBLP PIDs in publication affiliation evidence: {sorted(unknown_pids)}")
    provenance["buildInputs"] = {
        "publicationCategoryMappings": artifact_fingerprint(MAPPINGS_PATH),
        "publicationSnapshotIds": sorted(publication_snapshot_ids),
        "identitySnapshotId": entity_snapshot_id,
        "rawValuesPreserved": True,
    }
    if publication_affiliations_path.exists():
        provenance["buildInputs"]["publicationAffiliationEvidence"] = artifact_fingerprint(publication_affiliations_path)
    if automatic_path.exists():
        provenance["buildInputs"]["automaticAffiliationEvidence"] = artifact_fingerprint(automatic_path)
    for publication_group in publications.values():
        for paper in publication_group["papers"].values():
            paper["provenance"] = publication_provenance(provenance, paper)

    people: list[dict[str, Any]] = []
    cleanup_audit: list[dict[str, Any]] = []
    unique_main_keys: set[str] = set()
    unique_side_keys: set[str] = set()
    unique_class_keys: dict[str, set[str]] = defaultdict(set)
    total_service_events = 0

    for pid in sorted(db):
        d = db[pid]
        p = publications.get(
            pid, {"names": set(), "aliases": set(), "papers": {}}
        )
        names = set(d["names"]) | set(p["names"])
        name = choose_name(
            names,
            pid,
            preferred_names=set(d["preferred_names"]) | set(p["names"]),
        )

        raw_affiliations = unique(d["affiliations"])
        cleaned_affiliations, person_cleanup = clean_affiliations_with_audit(
            raw_affiliations
        )
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

        raw_current_affiliations = unique_preserve_order(
            d.get("current_affiliations_ordered") or d["current_affiliations"]
        )
        current_affiliation_pairs = [
            (raw, person_display_map.get(raw, clean_affiliation_label(raw)))
            for raw in raw_current_affiliations
        ]
        display_current_affiliations = unique_preserve_order(
            display for _raw, display in current_affiliation_pairs
        )
        raw_former_affiliations = unique(d["former_affiliations"])
        display_former_affiliations = unique(
            person_display_map.get(raw, clean_affiliation_label(raw))
            for raw in raw_former_affiliations
        )

        event_values: list[dict[str, Any]] = []
        for (venue, year, role), event in sorted(
            d["events"].items(),
            key=lambda item: (-(item[0][1] or 0), item[0][0], item[0][2]),
        ):
            raw_event_affiliations = unique(event["affiliations"])
            local_display, local_cleanup = clean_affiliations_with_audit(
                raw_event_affiliations
            )
            event_display_map = {
                raw: clean_affiliation_label(raw)
                for raw in raw_event_affiliations
            }
            event_reason_map: dict[str, str] = {}
            for change in local_cleanup:
                event_display_map[change["raw"]] = change["display"]
                event_reason_map[change["raw"]] = change["reason"]

            display_event_affiliations = unique(
                person_display_map.get(
                    raw,
                    event_display_map.get(raw, clean_affiliation_label(raw)),
                )
                for raw in raw_event_affiliations
            )
            for raw in raw_event_affiliations:
                display = person_display_map.get(
                    raw,
                    event_display_map.get(raw, clean_affiliation_label(raw)),
                )
                if raw == display:
                    continue
                reasons: list[str] = []
                if event_reason_map.get(raw):
                    reasons.append(event_reason_map[raw])
                if (
                    person_reason_map.get(raw)
                    and person_reason_map[raw] not in reasons
                ):
                    reasons.append(person_reason_map[raw])
                if person_reason_map.get(
                    raw
                ) and display != event_display_map.get(raw):
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
                        "reason": "+".join(dict.fromkeys(reasons))
                        or "person-canonical",
                    }
                )

            event_source_ids = unique(
                register_service_source(
                    provenance, url=url, venue=venue, year=year
                )
                for url in sorted(event["sources"])
            )
            event_method_ids = unique(
                service_method_id(source_db, venue)
                for source_db in event["source_dbs"]
            )
            if not event_method_ids:
                event_method_ids = [service_method_id("", venue)]
            event_affiliation_provenance = []
            for raw in raw_event_affiliations:
                display = person_display_map.get(
                    raw,
                    event_display_map.get(raw, clean_affiliation_label(raw)),
                )
                reasons = unique(
                    [event_reason_map.get(raw), person_reason_map.get(raw)]
                )
                event_affiliation_provenance.append(
                    {
                        "raw": raw,
                        "display": display,
                        "changed": raw != display,
                        "reason": "+".join(reasons) or "unchanged",
                    }
                )

            event_values.append(
                {
                    "venue": venue,
                    "year": year,
                    "role": role,
                    "rounds": unique(event["rounds"]),
                    "cycles": unique(event["cycles"]),
                    "affiliation": (
                        unique(event["affiliations"])[0]
                        if event["affiliations"]
                        else ""
                    ),
                    "rawAffiliations": raw_event_affiliations,
                    "displayAffiliations": display_event_affiliations
                    or local_display,
                    "displayAffiliation": (
                        (display_event_affiliations or local_display)[0]
                        if (display_event_affiliations or local_display)
                        else ""
                    ),
                    "sourceUrl": (
                        sorted(event["sources"])[0] if event["sources"] else ""
                    ),
                    "observationCount": event["observations"],
                    "provenance": {
                        "sourceIds": event_source_ids,
                        "extractionMethodIds": event_method_ids,
                        "identityMethodIds": ["person_entity_resolution"],
                        "transformationMethodIds": ["affiliation_display_cleaning"],
                        "affiliations": event_affiliation_provenance,
                    },
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
        unique_main_keys.update(paper["workId"] for paper in main_papers)
        unique_side_keys.update(paper["workId"] for paper in side_papers)
        for paper in papers:
            unique_class_keys[paper["publicationClass"]].add(paper["workId"])
        total_service_events += len(event_values)

        main_years = sorted({paper["year"] for paper in main_papers})
        side_years = sorted({paper["year"] for paper in side_papers})
        all_years = sorted(set(main_years) | set(side_years))
        main_venues = unique(paper["venue"] for paper in main_papers)
        publication_classes = unique(
            paper["publicationClass"] for paper in papers
        )
        class_counts = {
            class_name: sum(
                1 for paper in papers if paper["publicationClass"] == class_name
            )
            for class_name in PUBLICATION_CLASS_LABELS
        }

        primary_affiliation_evidence = choose_primary_affiliation_evidence(
            current_affiliation_pairs,
            event_values,
            cleaned_affiliations,
            affiliation_matcher=affiliation_matcher,
        )
        primary_affiliation_evidence = apply_automatic_evidence(
            primary_affiliation_evidence, automatic_affiliations.get(pid, {}), papers, provenance,
            matcher=affiliation_matcher, known_labels=cleaned_affiliations,
        )
        primary_affiliation_evidence = apply_publication_affiliation_evidence(
            primary_affiliation_evidence, publication_affiliations.get(pid, []), papers, provenance,
        )
        primary_affiliation = primary_affiliation_evidence["affiliation"]

        affiliation_provenance: list[dict[str, Any]] = []
        for raw in raw_affiliations:
            evidence = d["affiliation_evidence"].get(raw, {})
            source_ids: list[str] = []
            if evidence.get("dblp"):
                source_ids.append("dblp_snapshot")
            for source_url in sorted(evidence.get("service_urls", set())):
                source_ids.append(
                    register_service_source(
                        provenance, url=source_url, venue="", year=None
                    )
                )
            method_ids = unique(
                service_method_id(source_db, "")
                for source_db in evidence.get("source_dbs", set())
            )
            if evidence.get("dblp"):
                method_ids.append("person_entity_resolution")
            method_ids.append("affiliation_display_cleaning")
            display = person_display_map.get(raw, clean_affiliation_label(raw))
            affiliation_provenance.append(
                {
                    "raw": raw,
                    "display": display,
                    "changed": raw != display,
                    "reason": person_reason_map.get(raw) or "unchanged",
                    "sourceIds": unique(source_ids),
                    "methodIds": unique(method_ids),
                }
            )

        service_source_ids = unique(
            source_id
            for event in event_values
            for source_id in event.get("provenance", {}).get("sourceIds", [])
        )
        identity_statuses = unique(d["identity_statuses"])
        identity_source_ids = ["dblp_snapshot", *service_source_ids]
        identity_method_ids = ["person_entity_resolution"]
        if any("ror" in status for status in identity_statuses):
            identity_source_ids.append("ror_snapshot")
            identity_method_ids.append("ror_identity_evidence")

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
                "provenance": {
                    "identity": {
                        "sourceIds": unique(identity_source_ids),
                        "methodIds": unique(identity_method_ids),
                        "decisionKeys": identity_statuses,
                        "resolvedNameGroupCount": len(d["identity_name_keys"]),
                        "notes": unique(d["identity_notes"]),
                        "orcidEvidenceSources": unique(
                            d["identity_best_orcid_sources"]
                        ),
                    },
                    "serviceSourceIds": service_source_ids,
                    "primaryAffiliation": primary_affiliation_evidence,
                    "affiliations": affiliation_provenance,
                    "rawValuesPreserved": True,
                },
                "db": {
                    "venues": unique(d["venues"]),
                    "years": sorted(d["years"]),
                    "roles": unique(d["roles"]),
                    "serviceEventCount": len(event_values),
                    "observationCount": sum(
                        event["observationCount"] for event in event_values
                    ),
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

    db_name_groups = db_conn.execute(
        "SELECT COUNT(*) FROM name_resolution"
    ).fetchone()[0]
    db_resolved_groups = db_conn.execute(
        "SELECT COUNT(*) FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL"
    ).fetchone()[0]
    db_resolved_pids = db_conn.execute(
        "SELECT COUNT(DISTINCT resolved_dblp_pid) FROM name_resolution WHERE resolved_dblp_pid IS NOT NULL"
    ).fetchone()[0]

    changed_people = {
        row["dblpPid"] for row in cleanup_audit if row["scope"] == "person"
    }
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
        "mainCrossoverPeople": sum(
            1 for person in people if person["ml"]["hasMain"]
        ),
        "sideOnlyPeople": sum(
            1
            for person in people
            if person["ml"]["hasSide"] and not person["ml"]["hasMain"]
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
        "primaryAffiliationConflicts": sum(
            1
            for person in people
            if (person.get("provenance", {}).get("primaryAffiliation", {}).get("hasConflict"))
        ),
        "primaryAffiliationLowConfidence": sum(
            1
            for person in people
            if (person.get("provenance", {}).get("primaryAffiliation", {}).get("confidence") == "low")
        ),
        "primaryAffiliationReviewRows": sum(
            1
            for person in people
            if (
                person.get("provenance", {}).get("primaryAffiliation", {}).get("hasConflict")
                or person.get("provenance", {}).get("primaryAffiliation", {}).get("confidence") != "high"
            )
        ),
        "coverage": {
            "db": coverage(db_coverage),
            "mlMain": coverage(main_coverage),
        },
    }
    provenance["sources"] = dict(sorted(provenance["sources"].items()))
    provenance["coverage"] = {
        "officialServicePages": sum(
            1
            for source in provenance["sources"].values()
            if source.get("kind") == "official_service_page"
        ),
        "resolvedPeople": len(people),
        "serviceEvents": total_service_events,
        "canonicalWorks": len(unique_main_keys | unique_side_keys),
    }
    annotate_project_reviews(people, load_project_reviews(publications_csv.parent))
    return people, stats, cleanup_audit, provenance


def write_cleanup_audit(
    data_dir: Path, rows: list[dict[str, Any]]
) -> tuple[Path, Path]:
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
    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return csv_path, json_path



def primary_affiliation_review_rows(people: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return non-high-confidence primary-affiliation decisions for auditing.

    The report is diagnostic only: it never feeds back into affiliation selection.
    Values come from the DBLP/service/ROR data and reviewed publication evidence
    used by the site build.
    """
    rows: list[dict[str, Any]] = []
    for person in people:
        evidence = (person.get("provenance") or {}).get("primaryAffiliation") or {}
        confidence = clean(evidence.get("confidence"))
        has_conflict = bool(evidence.get("hasConflict"))
        if confidence == "high" and not has_conflict:
            continue
        decision_key = clean(evidence.get("decisionKey"))
        decision_rule = PRIMARY_AFFILIATION_DECISION_RULES.get(decision_key, {})
        rows.append(
            {
                "dblpPid": clean(person.get("dblpPid")),
                "personName": clean(person.get("name")),
                "selectedAffiliation": clean(person.get("primaryAffiliation")),
                "confidence": confidence or "low",
                "hasConflict": "yes" if has_conflict else "no",
                "decisionKey": decision_key,
                "decisionLabel": clean(decision_rule.get("label")),
                "dblpCurrentAffiliations": " | ".join(
                    unique_preserve_order(evidence.get("currentAffiliations") or [])
                ),
                "latestServiceYear": evidence.get("latestServiceYear") or "",
                "latestServiceAffiliations": " | ".join(
                    unique_preserve_order(evidence.get("latestServiceAffiliations") or [])
                ),
                "matchedServiceAffiliation": clean(evidence.get("matchedServiceAffiliation")),
                "publicationAffiliationYear": evidence.get("publicationAffiliationYear") or "",
                "publicationAffiliations": " | ".join(unique(
                    item["affiliation"] for item in evidence.get("publicationAffiliationEvidence", [])
                )),
                "publicationSourceUrls": " | ".join(unique(
                    item["sourceUrl"] for item in evidence.get("publicationAffiliationEvidence", [])
                )),
                "automaticAffiliationStatus": evidence.get("automaticAffiliationStatus", ""),
                "automaticAffiliationDate": evidence.get("automaticAffiliationDate", ""),
                "automaticPublicationUrls": " | ".join(unique(
                    item["sourceUrl"] for item in evidence.get("automaticAffiliationEvidence", [])
                )),
                "sourceIds": " | ".join(unique(evidence.get("sourceIds") or [])),
                "notes": " | ".join(unique_preserve_order(evidence.get("notes") or [])),
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            0 if row["hasConflict"] == "yes" else 1,
            {"low": 0, "medium": 1, "high": 2}.get(row["confidence"], 3),
            row["personName"].casefold(),
        ),
    )


def write_primary_affiliation_review(
    data_dir: Path, people: Iterable[dict[str, Any]]
) -> Path:
    reports = data_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    csv_path = reports / "primary_affiliation_review.csv"
    rows = primary_affiliation_review_rows(people)
    fields = [
        "dblpPid",
        "personName",
        "selectedAffiliation",
        "confidence",
        "hasConflict",
        "decisionKey",
        "decisionLabel",
        "dblpCurrentAffiliations",
        "latestServiceYear",
        "latestServiceAffiliations",
        "matchedServiceAffiliation",
        "publicationAffiliationYear",
        "publicationAffiliations",
        "publicationSourceUrls",
        "automaticAffiliationStatus",
        "automaticAffiliationDate",
        "automaticPublicationUrls",
        "sourceIds",
        "notes",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def browser_payload_and_detail_files(
    payload: dict[str, Any],
    additional_people: Iterable[dict[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, str], tuple[str, str] | None]:
    """Split heavy bibliography detail out of the startup browser payload.

    Main ML records stay eager because they power the landing-page filters and
    timelines. Other publication records are written as compact,
    content-addressed per-researcher JSON files and fetched only after a person
    dialog is opened. A separate title-only search index is also content-addressed
    and fetched only when a visitor searches, so broader paper-title lookup does
    not slow the initial page load. The readable ``site_data.json`` remains full.

    Additional matched PC profiles are exported separately for All/search. They
    share detail/search assets but never enter ``people`` or study statistics.
    """
    browser_payload = dict(payload)
    browser_meta = dict(payload.get("meta") or {})
    browser_meta.update(
        {
            "lazyPublicationDetails": True,
            "browserDetailSchemaVersion": BROWSER_DETAIL_SCHEMA_VERSION,
            "browserSearchSchemaVersion": BROWSER_SEARCH_SCHEMA_VERSION,
            "browserServiceSchemaVersion": 1,
            "browserPayloadDescription": (
                "Main ML papers load at startup; other publication "
                "details, researcher source evidence and broader paper-title search load on demand."
            ),
        }
    )
    browser_payload["meta"] = browser_meta

    browser_people: list[dict[str, Any]] = []
    additional_browser_people: list[dict[str, Any]] = []
    cohort_people = list(payload.get("people", []))
    cohort_ids = {person["id"] for person in cohort_people}
    seen_ids: set[str] = set()
    detail_files: dict[str, str] = {}
    search_people: dict[str, str] = {}
    for person in [*cohort_people, *additional_people]:
        if person["id"] in seen_ids:
            continue
        seen_ids.add(person["id"])
        ml = dict(person.get("ml") or {})
        eager_papers: list[dict[str, Any]] = []
        deferred_papers: list[dict[str, Any]] = []
        for paper in ml.get("papers", []):
            is_main = clean(paper.get("scope")).casefold() == "main"
            if is_main:
                eager_papers.append(paper)
            else:
                deferred_papers.append(paper)

        ml["papers"] = eager_papers
        ml["deferredPaperCount"] = len(deferred_papers)
        if deferred_papers:
            person_id = clean(person.get("id") or person.get("dblpPid"))
            detail_payload = {
                "schemaVersion": BROWSER_DETAIL_SCHEMA_VERSION,
                "personId": person_id,
                "paperCount": len(deferred_papers),
                "papers": deferred_papers,
            }
            detail_json = json.dumps(
                detail_payload, ensure_ascii=False, separators=(",", ":")
            )
            person_digest = hashlib.sha256(person_id.encode("utf-8")).hexdigest()[:12]
            content_digest = hashlib.sha256(detail_json.encode("utf-8")).hexdigest()[:12]
            filename = f"{person_digest}-{content_digest}.json"
            ml["detailUrl"] = f"data/people/{filename}"
            detail_files[filename] = detail_json + "\n"

            titles = [clean(paper.get("title")) for paper in deferred_papers]
            titles = [title for title in titles if title]
            if titles:
                search_people[person_id] = "\n".join(titles)

        browser_person = dict(person)
        browser_person["ml"] = ml
        # Evidence is not needed to search, filter or draw charts. Keep it in a
        # separate small file so opening a source tooltip never waits for a
        # researcher's potentially large bibliography.
        if person.get("provenance") or any(event.get("provenance") for event in (person.get("db") or {}).get("events", [])):
            evidence_json = json.dumps({
                "schemaVersion": 1,
                "personId": person["id"],
                "provenance": person.get("provenance") or {},
                "events": (person.get("db") or {}).get("events", []),
            }, ensure_ascii=False, separators=(",", ":"))
            person_digest = hashlib.sha256(person["id"].encode("utf-8")).hexdigest()[:12]
            digest = hashlib.sha256(evidence_json.encode("utf-8")).hexdigest()[:12]
            filename = f"evidence-{person_digest}-{digest}.json"
            browser_person["evidenceUrl"] = f"data/people/{filename}"
            browser_person.pop("provenance", None)
            detail_files[filename] = evidence_json + "\n"
        (browser_people if person["id"] in cohort_ids else additional_browser_people).append(browser_person)

    browser_payload["people"] = browser_people
    browser_payload["additionalExplorerPeople"] = additional_browser_people
    # The same service record appears in the researcher and classification
    # rosters. Transmit it once; app.js restores the ordinary events arrays
    # synchronously before any chart or filter reads them.
    service_events: list[dict[str, Any]] = []
    service_indices: dict[str, int] = {}

    def compact_services(person: dict[str, Any]) -> dict[str, Any]:
        compact = dict(person)
        if "db" not in person:
            return compact
        db = dict(person["db"])
        refs = []
        for event in db.pop("events", []):
            event = {key: value for key, value in event.items() if key != "provenance"}
            key = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key not in service_indices:
                service_indices[key] = len(service_events)
                service_events.append(event)
            refs.append(service_indices[key])
        db["eventRefs"] = refs
        compact["db"] = db
        return compact

    browser_payload["people"] = [compact_services(person) for person in browser_people]
    browser_payload["additionalExplorerPeople"] = [compact_services(person) for person in additional_browser_people]
    if "populationFocus" in payload:
        policy = dict(payload["populationFocus"])
        policy["candidates"] = [compact_services(person) for person in policy.get("candidates", [])]
        browser_payload["populationFocus"] = policy
    browser_payload["browserServiceEvents"] = service_events
    search_file: tuple[str, str] | None = None
    if search_people:
        search_payload = {
            "schemaVersion": BROWSER_SEARCH_SCHEMA_VERSION,
            "people": search_people,
        }
        search_json = json.dumps(
            search_payload, ensure_ascii=False, separators=(",", ":")
        )
        search_digest = hashlib.sha256(search_json.encode("utf-8")).hexdigest()[:12]
        search_filename = f"publication_search.{search_digest}.json"
        browser_meta["publicationSearchUrl"] = f"data/{search_filename}"
        search_file = (search_filename, search_json + "\n")

    return browser_payload, detail_files, search_file


def expand_browser_service_events(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore the transport-only service table for offline analysis tools."""
    events = payload.pop("browserServiceEvents", None)
    if events is None:
        return payload  # Previous exports already contain expanded events.
    for person in [*payload.get("people", []), *payload.get("additionalExplorerPeople", []),
                   *payload.get("populationFocus", {}).get("candidates", [])]:
        db = person.get("db", {})
        if "eventRefs" in db:
            db["events"] = [dict(events[index]) for index in db.pop("eventRefs")]
    return payload


def write_browser_detail_files(output_dir: Path, detail_files: dict[str, str]) -> None:
    detail_dir = output_dir / "people"
    detail_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in detail_files.items():
        target = detail_dir / filename
        if target.exists() and target.read_text(encoding="utf-8") == content:
            continue
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)


def write_browser_search_file(
    output_dir: Path, search_file: tuple[str, str] | None
) -> None:
    if search_file:
        filename, content = search_file
        target = output_dir / filename
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)


def write_browser_export(
    output_dir: Path, browser_payload: dict[str, Any],
    detail_files: dict[str, str], search_file: tuple[str, str] | None,
) -> None:
    """Publish immutable assets before their manifest; keep one prior generation."""
    target = output_dir / "site_data.js"
    previous_details: set[str] = set()
    previous_search: set[str] = set()
    can_prune = True
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";"))
            for person in [*previous.get("people", []), *previous.get("additionalExplorerPeople", [])]:
                url = person.get("ml", {}).get("detailUrl")
                if url:
                    previous_details.add(Path(url).name)
                if person.get("evidenceUrl"):
                    previous_details.add(Path(person["evidenceUrl"]).name)
            url = previous.get("meta", {}).get("publicationSearchUrl")
            if url:
                previous_search.add(Path(url).name)
        except (ValueError, IndexError, TypeError, AttributeError):
            # An unreadable old manifest cannot establish which assets are safe
            # to retire. Publishing a valid replacement still repairs the site.
            can_prune = False

    write_browser_detail_files(output_dir, detail_files)
    write_browser_search_file(output_dir, search_file)
    text = json.dumps(browser_payload, ensure_ascii=False, separators=(",", ":"))
    temporary = target.with_suffix(".js.tmp")
    temporary.write_text(f"window.CROSSOVER_DATA={text};\n", encoding="utf-8")
    temporary.replace(target)
    new_search = {search_file[0]} if search_file else set()
    assets_changed = set(detail_files) != previous_details or new_search != previous_search
    if can_prune and assets_changed:
        keep_details = set(detail_files) | previous_details
        for path in (output_dir / "people").glob("*.json"):
            if path.name not in keep_details:
                path.unlink()
        keep_search = previous_search | new_search
        for path in output_dir.glob("publication_search.*.json"):
            if path.name not in keep_search:
                path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--audit-output-dir", type=Path, default=BUILD_EXPORT_DIR,
                        help="Private readable exports (default: website/build).")
    parser.add_argument(
        "--allow-stale-inputs",
        action="store_true",
        help="Build even when reviewer/entity/publication artifacts are out of date.",
    )
    args = parser.parse_args()

    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    audit_dir = args.audit_output_dir.resolve()
    if audit_dir.is_relative_to(output_dir) or audit_dir.is_relative_to(WEBSITE_DIR.resolve()):
        raise SystemExit("Readable exports must stay outside the browser output directory.")
    db_path = data_dir / DB_ER
    raw_publications_csv = data_dir / RAW_PUBLICATIONS_CSV
    publications_csv = data_dir / PUBLICATIONS_CSV
    main_totals_csv = data_dir / MAIN_TOTALS_CSV
    arxiv_cache = data_dir / "reports" / ARXIV_CACHE_JSON

    if not db_path.exists():
        raise SystemExit(f"Missing required input: {db_path}")
    if not publications_csv.exists():
        raise SystemExit(
            f"Missing required input: {publications_csv}\n"
            "Run `python .\\scripts\\resolve_publication_entities.py --data-dir .\\data` first "
            "(after the DBLP publication scan)."
        )
    if not main_totals_csv.exists():
        raise SystemExit(
            f"Missing required input: {main_totals_csv}\n"
            "Run `python .\\scripts\\scan_dblp_publications.py --data-dir .\\data --force-scan` once to generate conference-size totals."
        )

    assert_pipeline_freshness(
        data_dir,
        entity_db=db_path,
        publications_csv=publications_csv,
        raw_publications_csv=raw_publications_csv,
        main_totals_csv=main_totals_csv,
        arxiv_cache=arxiv_cache,
        allow_stale=args.allow_stale_inputs,
    )

    main_venue_year_totals = load_main_venue_year_totals(main_totals_csv)

    affiliation_matcher = None
    ror_index_path = data_dir / "ror_snapshot" / ROR_INDEX_FILENAME
    if ror_index_path.exists():
        try:
            affiliation_matcher = RorAffiliationIndex(ror_index_path)
        except RuntimeError as exc:
            print(
                "WARNING: ROR affiliation matching unavailable for primary "
                f"affiliation selection: {exc}"
            )

    conn = sqlite3.connect(db_path)
    try:
        people, summary, cleanup_audit, provenance = build_payload(
            conn,
            publications_csv,
            affiliation_matcher=affiliation_matcher,
        )
    finally:
        conn.close()
        if affiliation_matcher is not None:
            affiliation_matcher.close()

    artifact_paths = {
        "focusVenueRules": FOCUS_RULES_PATH,
        "focusPublicationExclusions": FOCUS_EXCLUSIONS_PATH,
        "publicationCategoryMappings": MAPPINGS_PATH,
        "entityResolution": db_path,
        "publicationEntities": publications_csv,
        "mainMlVenueYearTotals": main_totals_csv,
    }
    for filename in [*REVIEW_FILES, ATTRIBUTIONS_FILE]:
        path = data_dir / filename
        if path.exists():
            artifact_paths[f"projectReview:{filename}"] = path
    publication_affiliations_path = data_dir / PUBLICATION_AFFILIATIONS_CSV
    if publication_affiliations_path.exists():
        artifact_paths["publicationAffiliationEvidence"] = publication_affiliations_path
    automatic_affiliations_path = data_dir / AUTOMATIC_AFFILIATIONS_JSON
    if automatic_affiliations_path.exists():
        artifact_paths["automaticAffiliationEvidence"] = automatic_affiliations_path
    if raw_publications_csv.exists():
        artifact_paths["rawPublicationScan"] = raw_publications_csv
    if arxiv_cache.exists():
        artifact_paths["arxivEvidenceCache"] = arxiv_cache
    provenance["buildInputs"]["inputFingerprint"] = combined_artifact_fingerprint(
        artifact_paths
    )
    service_sources_refreshed_at = latest_reviewer_fetch_at(data_dir)
    if service_sources_refreshed_at:
        provenance["buildInputs"]["serviceSourcesRefreshedAt"] = (
            service_sources_refreshed_at
        )
    arxiv_refreshed_at = arxiv_cache_refreshed_at(arxiv_cache)
    if arxiv_refreshed_at:
        provenance["sources"]["arxiv_metadata_cache"]["refreshedAt"] = arxiv_refreshed_at

    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    provenance["build"] = {
        "generatedAt": generated_at,
        "siteSchemaVersion": SCHEMA_VERSION,
        "provenanceSchemaVersion": PROVENANCE_SCHEMA_VERSION,
    }

    all_matched_people = people
    people, population_focus = apply_focus_population(people, data_dir)
    summary = cohort_summary(people, summary)
    provenance["coverage"]["resolvedPeople"] = len(people)
    provenance["coverage"]["serviceEvents"] = summary["dbServiceEvents"]
    provenance["coverage"]["canonicalWorks"] = len({p['workId'] for person in people for p in person['ml']['papers']})
    provenance["coverage"]["pcCandidatesBeforeFocus"] = population_focus["candidateCount"]
    provenance["methods"]["publication_focus"] = {
        "label": "DB-focused study population", "description": population_focus["definition"] + " " + population_focus["dbPaperPolicy"],
    }
    payload = {
        "meta": {
            "generatedAt": generated_at,
            "schemaVersion": SCHEMA_VERSION,
            "dataReady": True,
            "identityKey": "DBLP PID",
            "publicationSource": "Pinned DBLP snapshot + conservative publication entity resolution with cached exact-ID arXiv evidence for unresolved CoRR works",
            "publicationEntityResolution": "DBLP remains canonical; automatic merges use DBLP versionOf/shared DOI, conservative DBLP-only preprint rules, or cached exact-ID arXiv evidence. arXiv DOI evidence requires a unique archival DBLP target plus either an exact current-title match or at least 50% author overlap in both directions; current-title evidence requires a globally unique exact title/exact-author target within 0-2 years. Conflicting or weaker external evidence remains audit-only.",
            "mainVenueDefinition": "exact NeurIPS/NIPS, ICML, or ICLR main-conference DBLP record",
            "sidePaperDefinition": "other paper-like DBLP publication; context only, not ML crossover evidence",
            "publicationClasses": PUBLICATION_CLASS_LABELS,
            "publicationClassDefinition": "contextual classification from reviewed dated venue mappings and DBLP record metadata; only exact main-conference NeurIPS/NIPS, ICML, or ICLR records establish ML crossover membership",
            "defaultResearcherScope": "core",
            "populationFocusPolicyVersion": population_focus["version"],
            "affiliationDisplayCleaning": True,
            "affiliationCurrentFormerAware": True,
            "affiliationFuzzyBackend": fuzzy_backend(),
        },
        "summary": summary,
        "mainMlVenueYearTotals": main_venue_year_totals,
        "provenance": provenance,
        "populationFocus": population_focus,
        "people": people,
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    json_path = audit_dir / "site_data.json"
    js_path = output_dir / "site_data.js"
    # Stream the full bibliography before allocating browser detail strings.
    # A single giant Unicode string plus its UTF-8 copy can exhaust memory.
    temporary_json = json_path.with_suffix(".json.tmp")
    with temporary_json.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temporary_json.replace(json_path)
    pc_ids = {person["dblpPid"] for person in population_focus["candidates"]}
    additional_people = [person for person in all_matched_people
                         if person["dblpPid"] in pc_ids and not person["focus"]["included"]]
    browser_payload, detail_files, search_file = browser_payload_and_detail_files(payload, additional_people)
    write_home_population_summary(output_dir, people, main_venue_year_totals)
    export_coverage(data_dir, output_dir, all_matched_people, people, population_focus)

    cleanup_csv, cleanup_json = write_cleanup_audit(data_dir, cleanup_audit)
    affiliation_review_csv = write_primary_affiliation_review(data_dir, people)
    write_browser_export(output_dir, browser_payload, detail_files, search_file)

    print(f"Resolved DB people in site dataset: {summary['sitePeople']}")
    print(
        f"Core main-venue crossover people:  {summary['mainCrossoverPeople']}"
    )
    print(f"Unique Core ML papers:             {summary['uniqueMainPapers']}")
    print(f"Unique broader DBLP papers:        {summary['uniqueSidePapers']}")
    print(
        "Deferred browser publication detail: "
        f"{sum((person.get('ml') or {}).get('deferredPaperCount', 0) for person in browser_payload['people'])} papers "
        f"plus source evidence across {len(detail_files)} static files"
    )
    if search_file:
        print(f"Deferred broader-publication search index: {search_file[0]}")
    print("Publication classes (unique papers):")
    for class_name, label in PUBLICATION_CLASS_LABELS.items():
        print(
            f"  {label:<18} {summary['publicationClassCounts'][class_name]:>7}"
        )
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
    print(
        "Primary affiliation review: "
        f"{affiliation_review_csv} ({summary['primaryAffiliationReviewRows']} flagged rows)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
