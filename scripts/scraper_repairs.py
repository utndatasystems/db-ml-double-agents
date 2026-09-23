#!/usr/bin/env python3
"""Conservative repairs for known reviewer-scraper artifacts.

This module intentionally contains only exact, observed source artifacts.  It is
not a fuzzy person-name cleaner.  The raw scraper databases remain untouched;
`enrich_reviewers_with_dblp.py` applies these repairs only while building its
derived staging database and writes an audit log for every changed/rejected row.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveredReviewer:
    name: str
    affiliation: str = ""


@dataclass(frozen=True)
class ScraperRepair:
    action: str  # "repair", "reject", or staging-only recovery support
    name: str | None = None
    affiliation: str | None = None
    reason: str = "known_scraper_artifact"
    # For an exact known boundary-shift artifact, this fragment belongs to the
    # previous reviewer's affiliation rather than the current person's name.
    # If omitted, it can be derived from a leading prefix in raw_name.
    previous_affiliation_suffix: str | None = None
    # Exact people visibly concatenated into this row's affiliation field.
    # These are source-backed recoveries, not fuzzy guesses.
    recovered_reviewers: tuple[RecoveredReviewer, ...] = ()


# Exact names observed in the September 2026 unresolved audit.  Most are tails
# of an affiliation/location column that were glued to the following person's
# name by collapsed HTML/PDF text extraction.
KNOWN_NAME_REPAIRS: dict[str, ScraperRepair] = {
    "Amherst Sebastian Link": ScraperRepair("repair", "Sebastian Link", reason="leading_location_fragment"),
    "Arab Emirates Mohammad Sadoghi": ScraperRepair("repair", "Mohammad Sadoghi", reason="leading_location_fragment"),
    "Basilicata Chunbin Lin": ScraperRepair("repair", "Chunbin Lin", reason="leading_location_fragment"),
    "Berkeley Dingming Wu": ScraperRepair("repair", "Dingming Wu", reason="leading_location_fragment"),
    "Berlin Kostas Stefanidis": ScraperRepair("repair", "Kostas Stefanidis", reason="leading_location_fragment"),
    "Berlin Pinar Karagoz METU": ScraperRepair(
        "repair", "Pinar Karagoz", affiliation="METU, Turkey",
        reason="leading_location_and_trailing_affiliation_fragment",
        previous_affiliation_suffix="Berlin",
    ),
    "TU Berlin Pinar Karagoz METU": ScraperRepair(
        "repair", "Pinar Karagoz", affiliation="METU, Turkey",
        reason="leading_and_trailing_affiliation_fragments",
        previous_affiliation_suffix="TU Berlin",
    ),
    "Berlin Subhadeep Sarkar": ScraperRepair("repair", "Subhadeep Sarkar", reason="leading_location_fragment"),
    "Brunswick Tarique Siddiqui": ScraperRepair("repair", "Tarique Siddiqui", reason="leading_location_fragment"),
    "Calabria Fusheng Wang": ScraperRepair("repair", "Fusheng Wang", reason="leading_location_fragment"),
    "Charlotte Lukasz Golab": ScraperRepair("repair", "Lukasz Golab", reason="leading_location_fragment"),
    "Cities Mohammad Amiri": ScraperRepair("repair", "Mohammad Amiri", reason="leading_location_fragment"),
    "Dauphine Devis Bianchini": ScraperRepair("repair", "Devis Bianchini", reason="leading_institution_fragment"),
    "Davis Mohammad Javad Amiri": ScraperRepair("repair", "Mohammad Javad Amiri", reason="leading_location_fragment"),
    "Davis Pierangela Samarati": ScraperRepair("repair", "Pierangela Samarati", reason="leading_location_fragment"),
    "Delft Avigdor Gal": ScraperRepair("repair", "Avigdor Gal", reason="leading_location_fragment"),
    "Delft Barbara Catania": ScraperRepair("repair", "Barbara Catania", reason="leading_location_fragment"),
    "Delhi Michael Gertz": ScraperRepair("repair", "Michael Gertz", reason="leading_location_fragment"),
    "Dresden Dixin Tang": ScraperRepair("repair", "Dixin Tang", reason="leading_location_fragment"),
    "Eindhoven Matteo Lissandrini": ScraperRepair("repair", "Matteo Lissandrini", reason="leading_location_fragment"),
    "Eindhoven Oscar Romero": ScraperRepair("repair", "Oscar Romero", reason="leading_location_fragment"),
    "Eindhoven Panagiotis Karras": ScraperRepair("repair", "Panagiotis Karras", reason="leading_location_fragment"),
    "Emilia Hala Skaf-Molli": ScraperRepair("repair", "Hala Skaf-Molli", reason="leading_location_fragment"),
    "Emilia Lijun Chang": ScraperRepair("repair", "Lijun Chang", reason="leading_location_fragment"),
    "Group Sibo Wang": ScraperRepair("repair", "Sibo Wang", reason="leading_organization_fragment"),
    "Ilmenau Karthik Ramachandra": ScraperRepair("repair", "Karthik Ramachandra", reason="leading_location_fragment"),
    "Kaiserslautern Michael Mior": ScraperRepair("repair", "Michael Mior", reason="leading_location_fragment"),
    "Kaiserslautern Shantanu Sharma": ScraperRepair("repair", "Shantanu Sharma", reason="leading_location_fragment"),
    "Munich Slava Novgorodov": ScraperRepair("repair", "Slava Novgorodov", reason="leading_location_fragment"),
    "Munich Tingjian Ge": ScraperRepair("repair", "Tingjian Ge", reason="leading_location_fragment"),
    "Politecnica de Catalunya Evangelos Christidis": ScraperRepair("repair", "Evangelos Christidis", reason="leading_institution_fragment"),
    "Politècnica de Catalunya Alexandros Labrinidis": ScraperRepair("repair", "Alexandros Labrinidis", reason="leading_institution_fragment"),
    "Politècnica de Catalunya Patrick Damme": ScraperRepair("repair", "Patrick Damme", reason="leading_institution_fragment"),
    "Riverside Haibo Hu": ScraperRepair("repair", "Haibo Hu", reason="leading_location_fragment"),
    "Roma Tre Paolo Ciaccia": ScraperRepair("repair", "Paolo Ciaccia", reason="leading_institution_fragment"),
    "Roma Tre Sebastian Michel": ScraperRepair("repair", "Sebastian Michel", reason="leading_institution_fragment"),
    "Science Alekh Jindal": ScraperRepair(
        "repair",
        "Alekh Jindal",
        affiliation="Keebo",
        reason="leading_section_fragment_and_concatenated_next_person",
        recovered_reviewers=(RecoveredReviewer("Theodore Johnson", "AT&T Labs – Research"),),
    ),
    "Sciences Lei Cao": ScraperRepair("repair", "Lei Cao", reason="leading_institution_fragment"),
    "Sciences Yuncheng Wu": ScraperRepair("repair", "Yuncheng Wu", reason="leading_institution_fragment"),
    "U Calabria Alkis Simitsis": ScraperRepair("repair", "Alkis Simitsis", reason="leading_institution_fragment"),
    "Wales Yang Cao": ScraperRepair("repair", "Yang Cao", reason="leading_location_fragment"),
    "Web Services Spiros Skiadopoulos": ScraperRepair("repair", "Spiros Skiadopoulos", reason="leading_organization_fragment"),
    "Web Services Yanyan Shen": ScraperRepair("repair", "Yanyan Shen", reason="leading_organization_fragment"),
    "Papotti Paolo": ScraperRepair("repair", "Paolo Papotti", reason="reversed_person_name"),

    # This collapsed row contains a clean first person followed by another
    # person's name/affiliation in the affiliation field.  Keep only the part
    # we can repair with high confidence; do not synthesize a second reviewer.
    "AWS Cinzia Cappiello": ScraperRepair(
        "repair",
        "Cinzia Cappiello",
        affiliation="Politecnico di Milano",
        reason="leading_org_fragment_and_concatenated_next_person",
        recovered_reviewers=(RecoveredReviewer("Daniele Foroni", "Huawei"),),
    ),

    # Additional exact source-backed name repairs exposed by the audit.
    "Basu Roy": ScraperRepair("repair", "Senjuti Basu Roy", reason="dropped_given_name"),
    "C. Graham Cormode": ScraperRepair("repair", "Graham Cormode", reason="leading_affiliation_fragment"),
    "CL Lo": ScraperRepair("repair", "Eric CL Lo", reason="dropped_given_name"),
    "Hasan Haldar": ScraperRepair("repair", "Nur Al Hasan Haldar", reason="dropped_given_name_tokens"),
    "Lung Yiu": ScraperRepair("repair", "Man Lung Yiu", reason="dropped_given_name"),
    "Tien Tuan Anh Ding": ScraperRepair("repair", "Tien Tuan Anh Dinh", reason="source_name_typo"),
}


# Exact name+affiliation repairs for otherwise-valid names whose affiliation
# swallowed the next reviewer in a collapsed source block.  These are checked
# before the name-only table so they cannot affect clean observations elsewhere.
KNOWN_ROW_REPAIRS: dict[tuple[str, str], ScraperRepair] = {
    (
        "Anna Squicciarini",
        "The Pennsylvania State University Kostas Stefanidis",
    ): ScraperRepair(
        "repair",
        "Anna Squicciarini",
        affiliation="The Pennsylvania State University",
        reason="concatenated_next_person_in_affiliation",
        recovered_reviewers=(RecoveredReviewer("Kostas Stefanidis", ""),),
    ),
    (
        "Reza Akbarinia",
        "INRIA sainyam galhotra, UMass",
    ): ScraperRepair(
        "repair",
        "Reza Akbarinia",
        affiliation="INRIA",
        reason="concatenated_next_person_in_affiliation",
        recovered_reviewers=(RecoveredReviewer("Sainyam Galhotra", "UMass"),),
    ),
    (
        "Melanie Herschel",
        "Universität Stuttgart Vagelis Hristidis, UC",
    ): ScraperRepair(
        "repair",
        "Melanie Herschel",
        affiliation="Universität Stuttgart",
        reason="concatenated_next_person_in_affiliation",
        recovered_reviewers=(RecoveredReviewer("Vagelis Hristidis", "UC"),),
    ),
}


# Rows where the scraper put an institution/section label in the person-name
# field.  There is not enough information in that row to reconstruct a person,
# so these are rejected from entity matching rather than guessed.
KNOWN_REJECTED_NAME_ROWS: dict[str, ScraperRepair] = {
    ", Hong Kong University of Science and Technology": ScraperRepair("reject", reason="institution_in_name_field"),
    "Athens University of Economics and Business": ScraperRepair("reject", reason="institution_in_name_field"),
    "Data Analytics and Intelligence Lab": ScraperRepair("reject", reason="lab_in_name_field"),
    "Harbin Institute of Technology": ScraperRepair("reject", reason="institution_in_name_field"),
    "Harvard University": ScraperRepair("reject", reason="institution_in_name_field"),
    "Libre de Bruxelles": ScraperRepair("reject", reason="institution_fragment_in_name_field"),
    "Pennsylvania State University": ScraperRepair("reject", reason="institution_in_name_field"),
    "Politècnica de Catalunya": ScraperRepair("reject", reason="institution_fragment_in_name_field"),
    "Qatar Computing Research Institute": ScraperRepair("reject", reason="institution_in_name_field"),
    "School of Data Science": ScraperRepair("reject", reason="school_in_name_field"),
    "Shanghai Jiaotong University": ScraperRepair("reject", reason="institution_in_name_field"),
    "Universitat Rovira i Virgili": ScraperRepair("reject", reason="institution_in_name_field"),
    "University of Massachusetts": ScraperRepair("reject", reason="institution_in_name_field"),
    "University of Warsaw": ScraperRepair("reject", reason="institution_in_name_field"),
}


KNOWN_SCRAPER_REPAIRS: dict[str, ScraperRepair] = {
    **KNOWN_NAME_REPAIRS,
    **KNOWN_REJECTED_NAME_ROWS,
}


def repair_scraped_reviewer(name: str, affiliation: str) -> tuple[str, str, ScraperRepair | None]:
    """Return `(name, affiliation, applied_repair)` for an exact known artifact."""
    repair = KNOWN_ROW_REPAIRS.get((name, affiliation))
    if repair is None:
        repair = KNOWN_SCRAPER_REPAIRS.get(name)
    if repair is None:
        return name, affiliation, None
    if repair.action == "reject":
        return name, affiliation, repair
    return (
        repair.name or name,
        repair.affiliation if repair.affiliation is not None else affiliation,
        repair,
    )


def previous_affiliation_fragment(
    raw_name: str, repair: ScraperRepair | None
) -> str:
    """Return an exact fragment known to belong to the previous row's affiliation.

    This is deliberately enabled only for rows already present in the exact repair
    table.  It never tries to infer a boundary for an arbitrary scraped name.
    """
    if repair is None or repair.action != "repair":
        return ""
    if repair.previous_affiliation_suffix is not None:
        return repair.previous_affiliation_suffix.strip()
    repaired_name = (repair.name or "").strip()
    if not repaired_name or not raw_name.endswith(repaired_name):
        return ""
    if not repair.reason.startswith("leading_"):
        return ""
    return raw_name[: -len(repaired_name)].strip()
