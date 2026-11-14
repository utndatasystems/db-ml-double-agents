#!/usr/bin/env python3
r"""Audit unresolved DB reviewer -> DBLP identity candidates without mutating data.

This script reads the existing ``data/entity_resolution.sqlite`` produced by
``enrich_reviewers_with_dblp.py`` and ranks only the unresolved DBLP candidates
that are already present in that database. It does *not* rescan the DBLP XML
snapshot and it never writes resolution decisions back to SQLite.

The goal is to make a conservative Tier-2 review queue observable before any
matching rule is relaxed.

Outputs (under data/reports by default)
---------------------------------------
- unresolved_entity_resolution_candidates.csv
- unresolved_entity_resolution_audit.json
- unresolved_entity_resolution_summary.txt

Run from the repository root:

    python .\scripts\audit_unresolved_entity_resolution.py

The CSV is one row per unresolved reviewer-name group / DBLP PID candidate.
Rows with no current local DBLP candidate are retained with blank candidate
columns so the audit also shows the limits of the existing index.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from rapidfuzz import fuzz

try:
    from .display_cleaning import affiliation_equivalence_reason
except ImportError:
    from display_cleaning import affiliation_equivalence_reason

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
DEFAULT_DB = "entity_resolution.sqlite"
DEFAULT_REPORT_DIR = "reports"

SPACE_RE = re.compile(r"\s+")
NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
DBLP_MAGIC_SUFFIX_RE = re.compile(r"\s+\d{4}$")

AFFIL_STOPWORDS = {
    "the", "of", "and", "at", "for", "in", "on", "university",
    "universita", "universitat", "universite", "college", "school",
    "department", "dept", "faculty", "institute", "institution",
    "laboratory", "lab", "labs", "research", "center", "centre",
    "inc", "ltd", "llc", "group", "corporation", "corp",
}


SAFE_AFFILIATION_EQUIVALENCE_REASONS = {
    "normalized-exact",
    "abbreviation-alias",
    "branding-prefix",
    "location-suffix",
}

MATCH_PRIORITY = {
    "exact_alias": 3,
    "first_last_variant": 2,
    "initial_last_variant": 1,
}

BASE_SCORE = {
    "exact_alias": 76.0,
    "first_last_variant": 60.0,
    "initial_last_variant": 45.0,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return SPACE_RE.sub(" ", str(value).strip())


def normalize_name(value: Any) -> str:
    s = clean_text(value)
    s = DBLP_MAGIC_SUFFIX_RE.sub("", s)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("-", " ")
    s = NON_ALNUM_RE.sub(" ", s)
    return SPACE_RE.sub(" ", s).strip()


def normalize_affiliation(value: Any) -> str:
    s = clean_text(value)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return SPACE_RE.sub(" ", s).strip()


def affiliation_tokens(value: Any) -> set[str]:
    return {
        tok
        for tok in normalize_affiliation(value).split()
        if len(tok) >= 2 and tok not in AFFIL_STOPWORDS
    }


def json_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [clean_text(x) for x in value if clean_text(x)]
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(x) for x in parsed if clean_text(x)]


def pipe(values: Iterable[Any]) -> str:
    vals = sorted(
        {clean_text(v) for v in values if clean_text(v)},
        key=str.casefold,
    )
    return " | ".join(vals)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def require_schema(conn: sqlite3.Connection) -> None:
    required = {
        "name_groups": {
            "name_key", "representative_name", "observation_count",
            "min_year", "max_year", "venues_json", "roles_json",
            "affiliations_json",
        },
        "name_resolution": {
            "name_key", "representative_name", "status",
            "exact_candidate_count", "variant_candidate_count",
            "all_candidate_count", "notes",
        },
        "dblp_candidates": {
            "name_key", "dblp_pid", "match_type", "matched_alias",
            "affiliation_similarity",
        },
        "dblp_people": {
            "dblp_pid", "canonical_name", "aliases_json",
            "affiliations_json", "verified_orcids_json",
        },
    }
    problems: list[str] = []
    for table, columns in required.items():
        if not table_exists(conn, table):
            problems.append(f"missing table {table!r}")
            continue
        missing = columns - table_columns(conn, table)
        if missing:
            problems.append(
                f"table {table!r} missing columns: {', '.join(sorted(missing))}"
            )
    if problems:
        raise RuntimeError("entity-resolution schema mismatch: " + "; ".join(problems))


def first_last_key(value: Any) -> tuple[str, str]:
    toks = normalize_name(value).split()
    if len(toks) < 2:
        return "", ""
    return toks[0], toks[-1]


def initial_compatible(a: Any, b: Any) -> bool:
    af, al = first_last_key(a)
    bf, bl = first_last_key(b)
    return bool(af and bf and al and al == bl and af[0] == bf[0])


def exact_first_last(a: Any, b: Any) -> bool:
    af, al = first_last_key(a)
    bf, bl = first_last_key(b)
    return bool(af and bf and af == bf and al == bl)


def compatible_full_name(reviewer, candidate):
    """Allow omitted middle names/initials; reject different full given names."""
    left = normalize_name(reviewer).split()
    right = normalize_name(candidate).split()
    if len(left) < 2 or len(right) < len(left) or left[0] != right[0] or left[-1] != right[-1]:
        return False
    remaining = iter(right[1:-1])
    return all(any(a == b or (len(a) == 1 and b.startswith(a)) for b in remaining) for a in left[1:-1])


def best_name_similarity(reviewer: str, candidate_names: Iterable[str]) -> tuple[float, str]:
    reviewer_key = normalize_name(reviewer)
    best = 0.0
    best_name = ""
    for value in candidate_names:
        candidate_key = normalize_name(value)
        if not reviewer_key or not candidate_key:
            continue
        ratio = fuzz.ratio(reviewer_key, candidate_key) / 100.0
        token_ratio = fuzz.token_sort_ratio(reviewer_key, candidate_key) / 100.0
        score = max(ratio, token_ratio)
        if score > best:
            best = score
            best_name = clean_text(value)
    return best, best_name


@dataclass
class AffiliationEvidence:
    jaccard: float = 0.0
    fuzzy: float = 0.0
    exact_normalized: bool = False
    reviewer_value: str = ""
    candidate_value: str = ""


def best_affiliation_evidence(
    reviewer_affiliations: Iterable[str],
    candidate_affiliations: Iterable[str],
) -> AffiliationEvidence:
    best = AffiliationEvidence()
    for left in reviewer_affiliations:
        left_key = normalize_affiliation(left)
        left_tokens = affiliation_tokens(left)
        if not left_key:
            continue
        for right in candidate_affiliations:
            right_key = normalize_affiliation(right)
            right_tokens = affiliation_tokens(right)
            if not right_key:
                continue
            union = left_tokens | right_tokens
            jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
            fuzzy_score = fuzz.token_set_ratio(left_key, right_key) / 100.0
            exact = left_key == right_key

            # Prefer high token overlap; fuzzy similarity is a useful tie-breaker
            # but never replaces institution-specific token evidence.
            current = (jaccard, 1 if exact else 0, fuzzy_score)
            previous = (best.jaccard, 1 if best.exact_normalized else 0, best.fuzzy)
            if current > previous:
                best = AffiliationEvidence(
                    jaccard=jaccard,
                    fuzzy=fuzzy_score,
                    exact_normalized=exact,
                    reviewer_value=clean_text(left),
                    candidate_value=clean_text(right),
                )
    return best


def best_safe_affiliation_equivalence(
    reviewer_affiliations: Iterable[str],
    candidate_affiliations: Iterable[str],
) -> tuple[str, str, str]:
    """Return (reason, reviewer_label, candidate_label) for a safe equivalence.

    Only non-fuzzy equivalence rules are considered strong enough to influence
    the high-precision entity-resolution review queue.
    """
    for left in reviewer_affiliations:
        for right in candidate_affiliations:
            reason = affiliation_equivalence_reason(left, right)
            if reason in SAFE_AFFILIATION_EQUIVALENCE_REASONS:
                return reason, clean_text(left), clean_text(right)
    return "", "", ""


def best_match_type(match_types: Iterable[str]) -> str:
    return max(match_types, key=lambda x: MATCH_PRIORITY.get(x, 0), default="")


def score_candidate(
    match_type: str,
    name_similarity: float,
    affiliation_similarity: float,
    affiliation_exact: bool,
) -> float:
    score = BASE_SCORE.get(match_type, 35.0)

    if name_similarity >= 0.985:
        score += 6.0
    elif name_similarity >= 0.95:
        score += 4.0
    elif name_similarity >= 0.90:
        score += 2.0

    if affiliation_similarity >= 0.85:
        score += 18.0
    elif affiliation_similarity >= 0.65:
        score += 14.0
    elif affiliation_similarity >= 0.45:
        score += 9.0
    elif affiliation_similarity >= 0.25:
        score += 4.0

    if affiliation_exact:
        score += 3.0

    return min(score, 100.0)


def confidence_band(score: float, affiliation_similarity: float) -> str:
    if score >= 88 and affiliation_similarity >= 0.65:
        return "very_strong"
    if score >= 78 and affiliation_similarity >= 0.45:
        return "strong"
    if score >= 66:
        return "moderate"
    return "weak"


def classify_group(status: str, ranked: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (recommendation, explanation) for an unresolved name group."""
    if not ranked:
        return (
            "no_local_candidate",
            "No DBLP PID is present in the existing local candidate index; a broader snapshot matching rule is required before this case can be scored.",
        )

    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    margin = top["score"] - second["score"] if second else top["score"]
    candidate_count = len(ranked)
    aff = top["affiliation_similarity"]
    match_type = top["best_match_type"]
    safe_affiliation = bool(top.get("affiliation_safe_equivalence_reason"))

    # Proposed Tier-2 queue: only unique relaxed-name candidates with strong
    # independent affiliation support. This is intentionally stricter than the
    # existing resolver and is *not* applied automatically by this script.
    if candidate_count == 1:
        if (
            match_type == "first_last_variant"
            and top["first_last_exact"]
            and (aff >= 0.60 or safe_affiliation)
        ):
            return (
                "tier2_candidate",
                "Unique first/last-name variant with strong affiliation agreement; suitable for focused review before any automatic Tier-2 rule is enabled.",
            )
        if (
            match_type == "initial_last_variant"
            and aff >= 0.80
        ):
            return (
                "tier2_candidate",
                "Unique initial+last-name variant with very strong affiliation agreement; suitable for focused review.",
            )
        return (
            "manual_review_unique_candidate",
            "Only one local DBLP PID matches the relaxed name rule, but the independent affiliation evidence is not strong enough for the proposed Tier-2 queue.",
        )

    # Exact-name ambiguity should remain manual even with a good affiliation;
    # this queue simply surfaces cases where affiliation strongly separates one
    # candidate from the rest.
    if status == "ambiguous_exact_name":
        second_aff = second["affiliation_similarity"] if second else 0.0
        equivalent_candidates = [
            item for item in ranked
            if item.get("affiliation_safe_equivalence_reason")
        ]
        if safe_affiliation and len(equivalent_candidates) == 1:
            return (
                "high_precision_exact_affiliation_review",
                "Several DBLP PIDs share the exact normalized name, but exactly one candidate has a conservative affiliation-equivalence match. Keep read-only until this rule is reviewed on real cases.",
            )
        if aff >= 0.75 and margin >= 12.0 and second_aff <= 0.45:
            return (
                "strong_manual_review",
                "Several DBLP PIDs share the exact normalized name, but one candidate is strongly separated by affiliation evidence. Keep manual unless a dedicated exact-name disambiguation rule is adopted.",
            )
        return (
            "manual_review_ambiguous_exact",
            "Multiple DBLP identities share the exact normalized name; name evidence alone cannot safely resolve the person.",
        )

    if aff >= 0.65 and margin >= 12.0:
        return (
            "strong_manual_review",
            "Multiple relaxed-name candidates exist, but the top candidate is meaningfully separated by affiliation evidence.",
        )

    return (
        "manual_review_ambiguous",
        "Multiple plausible DBLP candidates remain without enough independent evidence to separate them safely.",
    )


def candidate_evidence_summary(row: dict[str, Any]) -> str:
    bits = [row["best_match_type"] or "no-name-rule"]
    bits.append(f"name={row['name_similarity']:.2f}")
    bits.append(f"aff={row['affiliation_similarity']:.2f}")
    if row["affiliation_exact_normalized"]:
        bits.append("aff-exact")
    if row.get("affiliation_safe_equivalence_reason"):
        bits.append(f"aff-safe={row['affiliation_safe_equivalence_reason']}")
    if row["last_name_exact"]:
        bits.append("last-exact")
    if row["first_last_exact"]:
        bits.append("first-last-exact")
    elif row["initial_compatible"]:
        bits.append("initial-compatible")
    return ", ".join(bits)


def with_nonperson_blockers(group: dict[str, Any]) -> dict[str, Any]:
    """Preserve old ambiguity guards without making non-person pages assignable.

    Legacy rules must still see missing evidence on a disambiguation page.
    Otherwise simply deleting it could turn an old rejection into an approval.
    The new affiliation stage deliberately evaluates only real candidates.
    """
    blockers = group.get("nonperson_candidates") or []
    if not blockers:
        return group
    return dict(group, candidates=list(group.get("candidates") or []) + blockers)


def load_audit_groups(conn: sqlite3.Connection, *, include_resolved: bool = False) -> list[dict[str, Any]]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    nonpeople = {}
    if "dblp_nonperson_candidates" in tables:
        for key, pid, kind, alias in conn.execute(
            "SELECT name_key,dblp_pid,match_type,matched_alias FROM dblp_nonperson_candidates"
        ):
            by_pid = nonpeople.setdefault(key, {})
            existing = by_pid.get(pid)
            if existing and existing["best_match_type"] == "exact_alias":
                continue
            by_pid[pid] = dict(pid=pid, canonical_name=alias, best_match_type=kind,
                               candidate_affiliations=[], verified_orcids=[],
                               affiliation_safe_equivalence_reason="", first_last_exact=False,
                               ror_matches={}, shared_ror_ids=[], nonperson=True)
    groups: list[dict[str, Any]] = []

    resolution_rows = conn.execute(
        """
        SELECT
            r.name_key,
            r.representative_name,
            r.status,
            r.exact_candidate_count,
            r.variant_candidate_count,
            r.all_candidate_count,
            r.notes,
            g.observation_count,
            g.min_year,
            g.max_year,
            g.venues_json,
            g.roles_json,
            g.affiliations_json,
            r.resolved_dblp_pid
        FROM name_resolution r
        JOIN name_groups g USING(name_key)
        WHERE (? OR r.resolved_dblp_pid IS NULL)
        ORDER BY r.representative_name COLLATE NOCASE, r.name_key
        """, (include_resolved,)
    ).fetchall()

    for row in resolution_rows:
        (
            name_key,
            representative_name,
            status,
            exact_candidate_count,
            variant_candidate_count,
            all_candidate_count,
            notes,
            observation_count,
            min_year,
            max_year,
            venues_json,
            roles_json,
            affiliations_json,
            resolved_pid,
        ) = row

        reviewer_affiliations = json_list(affiliations_json)
        candidate_rows = conn.execute(
            """
            SELECT
                c.dblp_pid,
                c.match_type,
                c.matched_alias,
                c.affiliation_similarity,
                p.canonical_name,
                p.aliases_json,
                p.affiliations_json,
                p.verified_orcids_json
            FROM dblp_candidates c
            JOIN dblp_people p ON p.dblp_pid=c.dblp_pid
            WHERE c.name_key=?
            ORDER BY c.dblp_pid, c.match_type, c.matched_alias
            """,
            (name_key,),
        ).fetchall()

        by_pid: dict[str, dict[str, Any]] = {}
        for (
            pid,
            match_type,
            matched_alias,
            stored_aff_sim,
            canonical_name,
            aliases_json,
            candidate_affiliations_json,
            orcids_json,
        ) in candidate_rows:
            item = by_pid.setdefault(
                pid,
                {
                    "pid": pid,
                    "match_types": set(),
                    "matched_aliases": set(),
                    "stored_affiliation_similarity": 0.0,
                    "canonical_name": clean_text(canonical_name),
                    "aliases": set(json_list(aliases_json)),
                    "candidate_affiliations": set(json_list(candidate_affiliations_json)),
                    "verified_orcids": set(json_list(orcids_json)),
                },
            )
            item["match_types"].add(match_type)
            item["matched_aliases"].add(clean_text(matched_alias))
            item["stored_affiliation_similarity"] = max(
                item["stored_affiliation_similarity"],
                float(stored_aff_sim or 0.0),
            )

        ranked: list[dict[str, Any]] = []
        for item in by_pid.values():
            candidate_names = set(item["aliases"]) | set(item["matched_aliases"])
            if item["canonical_name"]:
                candidate_names.add(item["canonical_name"])

            name_similarity, best_name = best_name_similarity(
                representative_name, candidate_names
            )
            aff_evidence = best_affiliation_evidence(
                reviewer_affiliations,
                item["candidate_affiliations"],
            )
            (
                affiliation_safe_reason,
                affiliation_safe_reviewer,
                affiliation_safe_candidate,
            ) = best_safe_affiliation_equivalence(
                reviewer_affiliations,
                item["candidate_affiliations"],
            )
            affiliation_similarity = max(
                item["stored_affiliation_similarity"],
                aff_evidence.jaccard,
            )
            best_type = best_match_type(item["match_types"])
            score = score_candidate(
                best_type,
                name_similarity,
                affiliation_similarity,
                aff_evidence.exact_normalized,
            )

            candidate_reference_name = best_name or item["canonical_name"]
            candidate = {
                "pid": item["pid"],
                "canonical_name": item["canonical_name"],
                "best_name": candidate_reference_name,
                "match_types": sorted(item["match_types"], key=lambda x: -MATCH_PRIORITY.get(x, 0)),
                "matched_aliases": sorted(item["matched_aliases"], key=str.casefold),
                "best_match_type": best_type,
                "name_similarity": round(name_similarity, 4),
                "first_last_exact": exact_first_last(representative_name, candidate_reference_name),
                "initial_compatible": initial_compatible(representative_name, candidate_reference_name),
                "last_name_exact": (
                    first_last_key(representative_name)[1]
                    == first_last_key(candidate_reference_name)[1]
                    and bool(first_last_key(representative_name)[1])
                ),
                "stored_affiliation_similarity": round(item["stored_affiliation_similarity"], 4),
                "affiliation_similarity": round(affiliation_similarity, 4),
                "affiliation_fuzzy_similarity": round(aff_evidence.fuzzy, 4),
                "affiliation_exact_normalized": aff_evidence.exact_normalized,
                "affiliation_safe_equivalence_reason": affiliation_safe_reason,
                "affiliation_safe_reviewer": affiliation_safe_reviewer,
                "affiliation_safe_candidate": affiliation_safe_candidate,
                "reviewer_affiliation_best": aff_evidence.reviewer_value,
                "candidate_affiliation_best": aff_evidence.candidate_value,
                "candidate_affiliations": sorted(item["candidate_affiliations"], key=str.casefold),
                "verified_orcids": sorted(item["verified_orcids"], key=str.casefold),
                "score": round(score, 2),
                "confidence": confidence_band(score, affiliation_similarity),
            }
            candidate["evidence"] = candidate_evidence_summary(candidate)
            ranked.append(candidate)

        ranked.sort(
            key=lambda x: (
                -x["score"],
                -x["affiliation_similarity"],
                -MATCH_PRIORITY.get(x["best_match_type"], 0),
                x["pid"],
            )
        )

        for index, candidate in enumerate(ranked, start=1):
            candidate["rank"] = index
        if ranked:
            second_score = ranked[1]["score"] if len(ranked) > 1 else 0.0
            ranked[0]["score_margin_to_second"] = round(ranked[0]["score"] - second_score, 2)
            for candidate in ranked[1:]:
                candidate["score_margin_to_second"] = ""

        recommendation, recommendation_reason = classify_group(status, ranked)
        groups.append(
            {
                "name_key": name_key,
                "representative_name": representative_name,
                "status": status,
                "resolved_dblp_pid": resolved_pid,
                "observation_count": observation_count,
                "min_year": min_year,
                "max_year": max_year,
                "venues": json_list(venues_json),
                "roles": json_list(roles_json),
                "reviewer_affiliations": reviewer_affiliations,
                "exact_candidate_count": exact_candidate_count,
                "variant_candidate_count": variant_candidate_count,
                "all_candidate_count": all_candidate_count,
                "source_notes": notes or "",
                "nonperson_candidates": list(nonpeople.get(name_key, {}).values()),
                "recommendation": recommendation,
                "recommendation_reason": recommendation_reason,
                "candidates": ranked,
            }
        )

    return groups


def flatten_rows(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in groups:
        candidates = group["candidates"] or [None]
        for candidate in candidates:
            row = {
                "name_key": group["name_key"],
                "reviewer_name": group["representative_name"],
                "resolution_status": group["status"],
                "recommendation": group["recommendation"],
                "recommendation_reason": group["recommendation_reason"],
                "reviewer_observation_count": group["observation_count"],
                "min_year": group["min_year"],
                "max_year": group["max_year"],
                "venues": pipe(group["venues"]),
                "roles": pipe(group["roles"]),
                "reviewer_affiliations": pipe(group["reviewer_affiliations"]),
                "exact_candidate_count": group["exact_candidate_count"],
                "variant_candidate_count": group["variant_candidate_count"],
                "all_candidate_count": group["all_candidate_count"],
                "source_resolution_notes": group["source_notes"],
            }
            if candidate is None:
                row.update(
                    {
                        "candidate_rank": "",
                        "candidate_pid": "",
                        "candidate_name": "",
                        "best_match_type": "",
                        "match_types": "",
                        "matched_aliases": "",
                        "name_similarity": "",
                        "first_last_exact": "",
                        "initial_compatible": "",
                        "last_name_exact": "",
                        "affiliation_similarity": "",
                        "affiliation_fuzzy_similarity": "",
                        "affiliation_exact_normalized": "",
                        "affiliation_safe_equivalence_reason": "",
                        "affiliation_safe_reviewer": "",
                        "affiliation_safe_candidate": "",
                        "reviewer_affiliation_best": "",
                        "candidate_affiliation_best": "",
                        "candidate_affiliations": "",
                        "verified_orcids": "",
                        "candidate_score": "",
                        "score_margin_to_second": "",
                        "confidence": "",
                        "evidence": "",
                    }
                )
            else:
                row.update(
                    {
                        "candidate_rank": candidate["rank"],
                        "candidate_pid": candidate["pid"],
                        "candidate_name": candidate["canonical_name"],
                        "best_match_type": candidate["best_match_type"],
                        "match_types": pipe(candidate["match_types"]),
                        "matched_aliases": pipe(candidate["matched_aliases"]),
                        "name_similarity": candidate["name_similarity"],
                        "first_last_exact": int(candidate["first_last_exact"]),
                        "initial_compatible": int(candidate["initial_compatible"]),
                        "last_name_exact": int(candidate["last_name_exact"]),
                        "affiliation_similarity": candidate["affiliation_similarity"],
                        "affiliation_fuzzy_similarity": candidate["affiliation_fuzzy_similarity"],
                        "affiliation_exact_normalized": int(candidate["affiliation_exact_normalized"]),
                        "affiliation_safe_equivalence_reason": candidate["affiliation_safe_equivalence_reason"],
                        "affiliation_safe_reviewer": candidate["affiliation_safe_reviewer"],
                        "affiliation_safe_candidate": candidate["affiliation_safe_candidate"],
                        "reviewer_affiliation_best": candidate["reviewer_affiliation_best"],
                        "candidate_affiliation_best": candidate["candidate_affiliation_best"],
                        "candidate_affiliations": pipe(candidate["candidate_affiliations"]),
                        "verified_orcids": pipe(candidate["verified_orcids"]),
                        "candidate_score": candidate["score"],
                        "score_margin_to_second": candidate.get("score_margin_to_second", ""),
                        "confidence": candidate["confidence"],
                        "evidence": candidate["evidence"],
                    }
                )
            rows.append(row)
    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else [
        "name_key", "reviewer_name", "resolution_status", "recommendation"
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(groups: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generatedAt": now_iso(),
        "purpose": "read-only unresolved DB reviewer -> DBLP candidate audit",
        "mutatesResolutionDatabase": False,
        "groups": groups,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_summary(groups: list[dict[str, Any]]) -> str:
    status_counts = Counter(group["status"] for group in groups)
    recommendation_counts = Counter(group["recommendation"] for group in groups)
    candidate_group_count = sum(bool(group["candidates"]) for group in groups)
    no_candidate_count = len(groups) - candidate_group_count
    tier2 = [g for g in groups if g["recommendation"] == "tier2_candidate"]
    high_precision_exact = [
        g for g in groups
        if g["recommendation"] == "high_precision_exact_affiliation_review"
    ]
    strong = [g for g in groups if g["recommendation"] == "strong_manual_review"]

    lines = [
        "Unresolved DB reviewer -> DBLP entity-resolution audit",
        "======================================================",
        f"Generated: {now_iso()}",
        "",
        f"Unresolved name groups: {len(groups)}",
        f"Groups with >=1 existing DBLP candidate: {candidate_group_count}",
        f"Groups with no existing local candidate: {no_candidate_count}",
        f"Proposed Tier-2 review candidates: {len(tier2)}",
        f"High-precision exact-name affiliation reviews: {len(high_precision_exact)}",
        f"Strong manual-review candidates: {len(strong)}",
        "",
        "Resolution statuses:",
    ]
    for status, count in sorted(status_counts.items(), key=lambda x: (-x[1], x[0])):
        lines.append(f"  {status:38s} {count:6d}")

    lines.extend(["", "Audit recommendations:"])
    for recommendation, count in sorted(
        recommendation_counts.items(), key=lambda x: (-x[1], x[0])
    ):
        lines.append(f"  {recommendation:38s} {count:6d}")

    def append_examples(title: str, items: list[dict[str, Any]], limit: int = 15) -> None:
        lines.extend(["", title])
        if not items:
            lines.append("  (none)")
            return
        ordered = sorted(
            items,
            key=lambda g: (
                -(g["candidates"][0]["score"] if g["candidates"] else -1),
                g["representative_name"].casefold(),
            ),
        )
        for group in ordered[:limit]:
            top = group["candidates"][0]
            aff = top["affiliation_similarity"]
            margin = top.get("score_margin_to_second", "")
            lines.append(
                f"  - {group['representative_name']} -> {top['canonical_name']} "
                f"[{top['pid']}] score={top['score']:.1f}, aff={aff:.2f}, "
                f"margin={margin}, rule={top['best_match_type']}"
            )

    append_examples("Top proposed Tier-2 review candidates:", tier2)
    append_examples("Top high-precision exact-name affiliation reviews:", high_precision_exact)
    append_examples("Top strong manual-review candidates:", strong)

    lines.extend(
        [
            "",
            "Interpretation:",
            "  - This report never changes resolved_dblp_pid.",
            "  - 'tier2_candidate' means review first; it is not an automatic resolution.",
            "  - 'no_local_candidate' cases need broader snapshot candidate generation before scoring can help.",
            "",
        ]
    )
    return "\n".join(lines)


def print_console_summary(groups: list[dict[str, Any]], csv_path: Path, json_path: Path, summary_path: Path) -> None:
    recs = Counter(g["recommendation"] for g in groups)
    statuses = Counter(g["status"] for g in groups)
    print(f"Unresolved reviewer name groups: {len(groups)}")
    for status, count in sorted(statuses.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {status:38s} {count:6d}")
    print("\nAudit queue")
    print("-----------")
    for rec, count in sorted(recs.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {rec:38s} {count:6d}")

    tier2 = [g for g in groups if g["recommendation"] == "tier2_candidate"]
    high_precision_exact = [
        g for g in groups
        if g["recommendation"] == "high_precision_exact_affiliation_review"
    ]
    if tier2:
        print("\nSample Tier-2 candidates")
        print("------------------------")
        for group in sorted(
            tier2,
            key=lambda g: -g["candidates"][0]["score"],
        )[:10]:
            top = group["candidates"][0]
            print(
                f"  - {group['representative_name']} -> {top['canonical_name']} "
                f"[{top['pid']}] score={top['score']:.1f}, "
                f"aff={top['affiliation_similarity']:.2f}, "
                f"{top['best_match_type']}"
            )

    if high_precision_exact:
        print("\nSample high-precision exact-name affiliation reviews")
        print("--------------------------------------------------")
        for group in sorted(
            high_precision_exact,
            key=lambda g: -g["candidates"][0]["score"],
        )[:10]:
            top = group["candidates"][0]
            print(
                f"  - {group['representative_name']} -> {top['canonical_name']} "
                f"[{top['pid']}] aff-rule={top['affiliation_safe_equivalence_reason']}, "
                f"{top['best_match_type']}"
            )

    print(f"\nCSV:     {csv_path}")
    print(f"JSON:    {json_path}")
    print(f"Summary: {summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Data directory (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Entity-resolution SQLite path (default: <data-dir>/entity_resolution.sqlite)",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Report directory (default: <data-dir>/reports)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = args.data_dir.resolve()
    db_path = (args.db or (data_dir / DEFAULT_DB)).resolve()
    report_dir = (args.report_dir or (data_dir / DEFAULT_REPORT_DIR)).resolve()

    if not db_path.exists():
        raise SystemExit(
            f"Entity-resolution database not found: {db_path}\n"
            "Run scripts/enrich_reviewers_with_dblp.py first."
        )

    csv_path = report_dir / "unresolved_entity_resolution_candidates.csv"
    json_path = report_dir / "unresolved_entity_resolution_audit.json"
    summary_path = report_dir / "unresolved_entity_resolution_summary.txt"

    conn = sqlite3.connect(db_path)
    try:
        require_schema(conn)
        groups = load_audit_groups(conn)
    finally:
        conn.close()

    rows = flatten_rows(groups)
    write_csv(rows, csv_path)
    write_json(groups, json_path)
    summary = build_summary(groups)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(summary, encoding="utf-8")

    print_console_summary(groups, csv_path, json_path, summary_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
