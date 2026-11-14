#!/usr/bin/env python3
"""Exact source-name corrections that are still observed in clean scraper output.

Keep this module intentionally tiny. Conference parsers should preserve people
from the source structurally; this layer is only for exact source-name issues
that remain after a clean scrape. It is not a fuzzy name cleaner and should not
contain migration artifacts from older parser bugs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScraperRepair:
    action: str
    name: str
    reason: str


KNOWN_NAME_REPAIRS: dict[str, ScraperRepair] = {
    # Source/order corrections still observed by the September 2026 audit.
    "Papotti Paolo": ScraperRepair(
        "repair",
        "Paolo Papotti",
        reason="reversed_person_name",
    ),
    "Tien Tuan Anh Ding": ScraperRepair(
        "repair",
        "Tien Tuan Anh Dinh",
        reason="source_name_typo",
    ),
    "H. V. Jadadish": ScraperRepair(
        "repair",
        "H. V. Jagadish",
        reason="source_name_typo",
    ),
    "Amine Mhedhbi to Polytechnique Montreal": ScraperRepair(
        "repair",
        "Amine Mhedhbi",
        reason="source_name_annotation",
    ),
}


def repair_scraped_reviewer(
    name: str,
    affiliation: str,
) -> tuple[str, str, ScraperRepair | None]:
    """Apply an exact observed source-name correction, if one exists."""
    repair = KNOWN_NAME_REPAIRS.get(name)
    if repair is None:
        return name, affiliation, None
    return repair.name, affiliation, repair
