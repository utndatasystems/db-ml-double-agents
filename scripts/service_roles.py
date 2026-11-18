#!/usr/bin/env python3
"""Shared service-role normalization and scraper replacement safety.

Raw conference labels remain untouched in ``reviewer_records.role``.  This
module adds three orthogonal fields used by downstream logic:

* ``role_family``: normalized role category used across venues.
* ``track``: research/conference/industrial/etc. service context.
* ``qualifies_db_seed``: whether the service event can seed the DB-researcher
  population used by entity resolution.

Unknown roles are deliberately non-qualifying.  A newly scraped role therefore
has to be classified explicitly before it can add people to the study.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class ServiceRoleMetadata:
    role_family: str
    track: str
    qualifies_db_seed: int


class ServiceRecord(Protocol):
    role: str
    name_key: str


# Existing 2022+ reviewer roles.  Every role currently used to construct the
# dataset remains qualifying, so adding the schema cannot remove existing
# people from entity resolution.
_ROLE_MAP: dict[str, ServiceRoleMetadata] = {
    "associate_editor": ServiceRoleMetadata("senior_reviewer", "research", 1),
    "area_chair": ServiceRoleMetadata("senior_reviewer", "research", 1),
    "associate_chair": ServiceRoleMetadata("senior_reviewer", "research", 1),
    "senior_program_committee_member": ServiceRoleMetadata("senior_reviewer", "research", 1),
    "pc_member": ServiceRoleMetadata("research_reviewer", "research", 1),
    "program_committee_member": ServiceRoleMetadata("research_reviewer", "research", 1),
    "review_board_member": ServiceRoleMetadata("research_reviewer", "research", 1),
    "program_committee_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    "program_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    "program_co_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    "research_program_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    "research_program_co_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    "program_vice_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    "pc_track_chair": ServiceRoleMetadata("research_program_chair", "research", 1),
    # PVLDB Editors-in-Chief serve as the research-program leadership layer.
    "editor_in_chief": ServiceRoleMetadata("research_program_chair", "research", 1),
    "general_chair": ServiceRoleMetadata("general_chair", "conference", 1),
    "general_co_chair": ServiceRoleMetadata("general_chair", "conference", 1),
    # Conference-level technical-track service is strong evidence that a person
    # is already participating in the database research community. Chairs and
    # members of flagship industrial/application and demo PCs therefore qualify.
    # Tutorial/workshop PC membership stays non-seeding; their chairs are
    # leadership roles and continue to qualify.
    "industrial_track_chair": ServiceRoleMetadata("technical_track_chair", "industrial", 1),
    "demo_chair": ServiceRoleMetadata("technical_track_chair", "demo", 1),
    "demonstration_chair": ServiceRoleMetadata("technical_track_chair", "demo", 1),
    "tutorial_chair": ServiceRoleMetadata("technical_track_chair", "tutorial", 1),
    "workshop_chair": ServiceRoleMetadata("technical_track_chair", "workshop", 1),
    "phd_symposium_chair": ServiceRoleMetadata("technical_track_chair", "phd_symposium", 1),
    "panel_chair": ServiceRoleMetadata("technical_track_chair", "panel", 1),
    "special_session_chair": ServiceRoleMetadata("technical_track_chair", "special_session", 1),
    "lightning_talks_chair": ServiceRoleMetadata("technical_track_chair", "lightning_talks", 1),
    "review_quality_chair": ServiceRoleMetadata("technical_track_chair", "review_quality", 1),
    "industrial_pc_member": ServiceRoleMetadata("technical_track_reviewer", "industrial", 1),
    "demo_pc_member": ServiceRoleMetadata("technical_track_reviewer", "demo", 1),
    "demo_associate_editor": ServiceRoleMetadata("senior_reviewer", "demo", 1),
    "industrial_associate_editor": ServiceRoleMetadata("senior_reviewer", "industrial", 1),
    "tutorial_pc_member": ServiceRoleMetadata("technical_track_reviewer", "tutorial", 0),
    "workshop_pc_member": ServiceRoleMetadata("technical_track_reviewer", "workshop", 0),
    # PODS is the co-located SIGMOD database-theory flagship and already appears
    # in the publication-side database venue list.  Its PC is therefore a DB
    # research-service seed just like the SIGMOD research PC.
    "pods_program_chair": ServiceRoleMetadata("research_program_chair", "pods", 1),
    "pods_pc_member": ServiceRoleMetadata("research_reviewer", "pods", 1),
    # Award/Test-of-Time committees are small senior technical-service roles.
    "award_committee_member": ServiceRoleMetadata("senior_reviewer", "awards", 1),
    # Explicitly non-scientific conference service.
    "publicity_chair": ServiceRoleMetadata("administrative_service", "conference", 0),
    "web_chair": ServiceRoleMetadata("administrative_service", "conference", 0),
    "registration_chair": ServiceRoleMetadata("administrative_service", "conference", 0),
    "finance_chair": ServiceRoleMetadata("administrative_service", "conference", 0),
    "sponsorship_chair": ServiceRoleMetadata("administrative_service", "conference", 0),
    "local_arrangements_chair": ServiceRoleMetadata("administrative_service", "conference", 0),
}


def normalize_role_token(value: str) -> str:
    token = (value or "").strip().casefold().replace("&", "and")
    for char in ("-", "/", " "):
        token = token.replace(char, "_")
    while "__" in token:
        token = token.replace("__", "_")
    return token.strip("_")


def classify_service_role(
    venue: str,
    raw_role: str,
    *,
    existing_track: str = "",
) -> ServiceRoleMetadata:
    """Return normalized metadata for one raw service role.

    ``venue`` is accepted intentionally even though the current mappings are
    cross-venue: later venue-specific exceptions can be added centrally rather
    than duplicated across five scrapers.
    """
    del venue  # reserved for venue-specific mappings
    role = normalize_role_token(raw_role)
    metadata = _ROLE_MAP.get(role)
    if metadata is None:
        return ServiceRoleMetadata(
            role_family="other_service",
            track=(existing_track or "conference").strip().casefold(),
            qualifies_db_seed=0,
        )

    # Preserve a more specific source-provided track when it exists.  Current
    # reviewer databases use "research", so this is backwards compatible.
    track = (existing_track or metadata.track).strip().casefold()
    return ServiceRoleMetadata(metadata.role_family, track, metadata.qualifies_db_seed)


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def ensure_reviewer_service_schema(
    conn: sqlite3.Connection,
    *,
    venue: str,
    table: str = "reviewer_records",
) -> None:
    """Add/populate normalized service columns without changing raw records."""
    columns = table_columns(conn, table)
    with conn:
        if "role_family" not in columns:
            conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN role_family TEXT NOT NULL DEFAULT ""'
            )
        if "track" not in columns:
            conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN track TEXT NOT NULL DEFAULT ""'
            )
        if "qualifies_db_seed" not in columns:
            conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN qualifies_db_seed INTEGER NOT NULL DEFAULT 0'
            )

        rows = conn.execute(
            f'SELECT id, role, track FROM "{table}"'
        ).fetchall()
        updates: list[tuple[str, str, int, int]] = []
        for row_id, role, track in rows:
            metadata = classify_service_role(
                venue,
                str(role or ""),
                existing_track=str(track or ""),
            )
            updates.append(
                (metadata.role_family, metadata.track, metadata.qualifies_db_seed, int(row_id))
            )
        if updates:
            conn.executemany(
                f'UPDATE "{table}" SET role_family=?, track=?, qualifies_db_seed=? WHERE id=?',
                updates,
            )

        conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{table}_qualifying_seed '
            f'ON "{table}"(qualifies_db_seed)'
        )


def service_values(venue: str, record: ServiceRecord) -> tuple[str, str, int]:
    metadata = classify_service_role(venue, record.role)
    return metadata.role_family, metadata.track, metadata.qualifies_db_seed


def assert_no_qualifying_person_loss(
    conn: sqlite3.Connection,
    records: Iterable[ServiceRecord],
    *,
    venue: str,
    year_column: str,
    year: int,
    allow_loss: bool = False,
    table: str = "reviewer_records",
    previous_key_equivalents: dict[str, set[str]] | None = None,
    ignored_previous_keys: Iterable[str] = (),
) -> None:
    """Abort replacement if a rerun would drop a previously qualifying person.

    The comparison uses each scraper's conservative ``name_key``.  It is
    intentionally strict: source/parser changes that rename or remove a person
    require inspection before old data can be replaced.
    """
    if allow_loss:
        return

    previous = {
        str(row[0])
        for row in conn.execute(
            f'SELECT DISTINCT name_key FROM "{table}" '
            f'WHERE "{year_column}"=? AND qualifies_db_seed=1',
            (year,),
        )
        if row[0]
    }
    if not previous:
        return

    incoming: set[str] = set()
    for record in records:
        if classify_service_role(venue, record.role).qualifies_db_seed:
            key = (record.name_key or "").strip()
            if key:
                incoming.add(key)

    equivalents = previous_key_equivalents or {}
    ignored = {str(key).strip() for key in ignored_previous_keys if str(key).strip()}
    missing: list[str] = []
    for key in sorted(previous):
        if key in incoming or key in ignored:
            continue
        if incoming.intersection(equivalents.get(key, set())):
            continue
        missing.append(key)
    if not missing:
        return

    preview = ", ".join(missing[:12])
    suffix = "" if len(missing) <= 12 else f", ... (+{len(missing) - 12} more)"
    raise RuntimeError(
        f"Refusing to replace {venue} {year}: {len(missing)} previously qualifying "
        f"person(s) are missing from the new parse: {preview}{suffix}. "
        "Inspect the source/parser change first. If the loss is intentional, rerun "
        "with --allow-qualifying-loss."
    )
