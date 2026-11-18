"""Explicit, reviewable display-category mappings for contextual publications.

Exact rows precede namespace prefixes; ties follow CSV order. Main ML membership and CoRR/preprint recognition
remain protected by the site builder, outside this display-only mapping table.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MAPPINGS_PATH = Path(__file__).resolve().parents[1] / "data" / "publication_category_mappings.csv"
SIDE_CATEGORIES = {"database", "other_conference", "journal", "workshop", "preprint", "other"}
FIELDS = {"dblp_key", "booktitle", "journal", "venue_label"}


def normalized(value: str) -> str:
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class CategoryMapping:
    rule_id: str
    match_field: str
    match_type: str
    match_text: str
    category: str
    venue_label: str
    evidence_url: str
    reason: str
    year_from: str = ""
    year_to: str = ""

    def matches(self, values: dict[str, str]) -> bool:
        if self.year_from or self.year_to:
            try:
                year = int(values.get("year", ""))
            except (ValueError, TypeError):
                return False
            if self.year_from and year < int(self.year_from):
                return False
            if self.year_to and year > int(self.year_to):
                return False
        actual = normalized(values.get(self.match_field, ""))
        expected = normalized(self.match_text)
        return actual.startswith(expected) if self.match_type == "prefix" else actual == expected


def load_category_mappings(path: Path = MAPPINGS_PATH) -> tuple[CategoryMapping, ...]:
    rules = []
    ids = set()
    targets = set()
    ranges = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = set(CategoryMapping.__dataclass_fields__)
        if set(reader.fieldnames or ()) != required:
            raise ValueError(f"{path}: expected CSV columns {sorted(required)}")
        for line, row in enumerate(reader, 2):
            if not any(row.values()):
                continue
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"{path}:{line}: malformed CSV row")
            rule = CategoryMapping(**{key: value.strip() for key, value in row.items()})
            if not all((rule.rule_id, rule.match_text, rule.evidence_url, rule.reason)):
                raise ValueError(f"{path}:{line}: rule ID, match text, evidence URL, and reason are required")
            if rule.category not in SIDE_CATEGORIES or rule.match_field not in FIELDS:
                raise ValueError(f"{path}:{line}: unsupported category or field")
            if rule.match_type not in {"exact", "prefix"}:
                raise ValueError(f"{path}:{line}: match type must be exact or prefix")
            if rule.match_type == "prefix" and (rule.match_field != "dblp_key" or not rule.match_text.endswith("/")):
                raise ValueError(f"{path}:{line}: prefixes must be complete DBLP namespaces ending in /")
            if not rule.evidence_url.startswith("https://"):
                raise ValueError(f"{path}:{line}: evidence URL must use HTTPS")
            for bound in (rule.year_from, rule.year_to):
                if bound and (not bound.isdecimal() or int(bound) < 1):
                    raise ValueError(f"{path}:{line}: year bounds must be positive integers")
            if rule.year_from and rule.year_to and int(rule.year_from) > int(rule.year_to):
                raise ValueError(f"{path}:{line}: year_from exceeds year_to")
            target = (rule.match_field, rule.match_type, normalized(rule.match_text), rule.year_from, rule.year_to)
            if rule.rule_id in ids or target in targets:
                raise ValueError(f"{path}:{line}: duplicate rule ID or match target")
            base = target[:3]
            lower, upper = int(rule.year_from or 0), int(rule.year_to or 99999)
            for previous_lower, previous_upper in ranges.get(base, []):
                if max(lower, previous_lower) <= min(upper, previous_upper):
                    raise ValueError(f"{path}:{line}: overlapping year ranges for the same match target")
            ranges.setdefault(base, []).append((lower, upper))
            ids.add(rule.rule_id)
            targets.add(target)
            rules.append(rule)
    return tuple(rules)


@lru_cache(maxsize=1)
def default_category_mappings() -> tuple[CategoryMapping, ...]:
    return load_category_mappings()


@lru_cache(maxsize=1)
def _mapping_index() -> tuple[dict, list]:
    exact = {}
    prefixes = []
    for index, rule in enumerate(default_category_mappings()):
        if rule.match_type == "prefix":
            prefixes.append((index, rule))
        else:
            exact.setdefault((rule.match_field, normalized(rule.match_text)), []).append((index, rule))
    return exact, prefixes


def matching_category_rule(**values: str) -> CategoryMapping | None:
    exact, prefixes = _mapping_index()
    candidates = list(prefixes)
    for field in FIELDS:
        candidates.extend(exact.get((field, normalized(values.get(field, ""))), ()))
    return next((rule for _, rule in sorted(candidates, key=lambda item: (item[1].match_type == "prefix", item[0])) if rule.matches(values)), None)
