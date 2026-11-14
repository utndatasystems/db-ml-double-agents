"""Small shared helpers for exact arXiv identifiers in publication metadata."""

from __future__ import annotations

import re
from typing import Iterable

ARXIV_ID_RE = re.compile(
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.\-]*/\d{7}))(?:v\d+)?",
    re.IGNORECASE,
)
ARXIV_CONTEXT_RE = re.compile(
    r"(?:arxiv\.org/(?:abs|pdf)/|(?:doi\.org/10\.48550/)?arxiv[.:/])"
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.\-]*/\d{7}))(?:v\d+)?",
    re.IGNORECASE,
)
DBLP_MODERN_CORR_KEY_RE = re.compile(
    r"^journals/corr/abs-(\d{4})-(\d{4,5})$",
    re.IGNORECASE,
)
DBLP_LEGACY_CORR_KEY_RE = re.compile(
    r"^journals/corr/(?P<archive>[a-z][a-z0-9-]*?)(?:-[A-Z][A-Z0-9.-]*)?-(?P<number>\d{7})$"
)
LEGACY_SUBJECT_ID_RE = re.compile(
    r"^(?P<archive>[a-z][a-z0-9-]*)\.[a-z0-9.-]+/(?P<number>\d{7})$",
    re.IGNORECASE,
)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def canonicalize_arxiv_id(value: str) -> str:
    """Return the canonical article id used by the arXiv API.

    DBLP occasionally exposes old subject-qualified identifiers such as
    ``cs.IR/0407061``. The arXiv article/API identifier is ``cs/0407061``;
    the subject class is metadata, not part of the stable article id.
    """
    value = _clean(value)
    if not value:
        return ""
    value = re.sub(r"v\d+$", "", value, flags=re.IGNORECASE)
    legacy = LEGACY_SUBJECT_ID_RE.fullmatch(value)
    if legacy:
        return f"{legacy.group('archive').casefold()}/{legacy.group('number')}"
    if "/" in value:
        archive, number = value.split("/", 1)
        return f"{archive.casefold()}/{number}"
    return value


def normalize_arxiv_id(value: str) -> str:
    value = _clean(value)
    if not value:
        return ""
    match = ARXIV_CONTEXT_RE.search(value)
    if match:
        return canonicalize_arxiv_id(match.group("id"))
    match = ARXIV_ID_RE.fullmatch(value)
    if match:
        return canonicalize_arxiv_id(match.group("id"))
    return ""


def extract_arxiv_id(*values: str) -> str:
    """Extract a canonical arXiv id from URLs/DOIs/identifier strings."""
    for value in values:
        value = _clean(value)
        if not value:
            continue
        match = ARXIV_CONTEXT_RE.search(value)
        if match:
            return canonicalize_arxiv_id(match.group("id"))

    for value in values:
        value = _clean(value)
        match = ARXIV_ID_RE.fullmatch(value)
        if match:
            return canonicalize_arxiv_id(match.group("id"))
    return ""


def arxiv_id_from_publication_values(
    *,
    url: str = "",
    ees: Iterable[str] = (),
    dblp_key: str = "",
) -> str:
    """Extract an exact arXiv id already present in DBLP publication metadata."""
    arxiv_id = extract_arxiv_id(url, *list(ees))
    if arxiv_id:
        return arxiv_id

    key = _clean(dblp_key)
    modern = DBLP_MODERN_CORR_KEY_RE.fullmatch(key)
    if modern:
        return f"{modern.group(1)}.{modern.group(2)}"

    legacy = DBLP_LEGACY_CORR_KEY_RE.fullmatch(key)
    if legacy:
        return f"{legacy.group('archive').casefold()}/{legacy.group('number')}"
    return ""
