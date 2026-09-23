#!/usr/bin/env python3
"""Display-only cleanup helpers for website-facing metadata.

This module intentionally does *not* participate in entity resolution and does
not mutate source databases. It turns already-resolved metadata into cleaner
labels for presentation while preserving the raw values elsewhere in the site
payload.

The cleaner is deliberately conservative. It only auto-merges labels when the
relationship is high confidence. ``clean_affiliations_with_audit`` exposes the
mapping and rule used for every changed label so cleanup remains inspectable.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any, Iterable

try:
    from rapidfuzz import fuzz as _rapidfuzz
except ImportError:  # Keep the site build functional without optional package.
    _rapidfuzz = None


_SPLIT_RE = re.compile(r"\s*(?:,|;|\||·)\s*")
_ORG_UNIT_RE = re.compile(
    r"\b(?:department|dept|school|faculty|institute|institution|laboratory|lab|"
    r"centre|center|group|research|division|college|hospital|clinic|team|unit|"
    r"chair|program|programme|school|ecole|école|polytechnic|polytechnique|"
    r"faculte|faculté|fakultat|fakultät|departement|département|laboratoire)\b",
    re.IGNORECASE,
)

# Comparison-only abbreviation expansion. Display text is never rewritten from
# this map; it only helps us recognize very close variants.
_TOKEN_EXPANSIONS = {
    "univ": "university",
    "universitaet": "university",
    "universitat": "university",
    "inst": "institute",
    "institut": "institute",
    "dept": "department",
    "ctr": "center",
    "centre": "center",
    "lab": "laboratory",
    "labs": "laboratories",
    "corp": "corporation",
    "tu": "technical university",
}

# Deliberately small and conservative. These are only used to recognize that a
# comma-separated suffix is geographical. Missing a country here means we keep
# two display values rather than risking a false merge.
_LOCATION_MARKERS = {
    "argentina", "australia", "austria", "belgium", "brazil", "canada",
    "chile", "china", "croatia", "cyprus", "czech republic", "czechia",
    "denmark", "estonia", "finland", "france", "germany", "greece",
    "hong kong", "hungary", "iceland", "india", "ireland", "israel",
    "italy", "japan", "korea", "south korea", "latvia", "lithuania",
    "luxembourg", "mexico", "netherlands", "the netherlands", "new zealand", "norway",
    "poland", "portugal", "romania", "singapore", "slovakia", "slovenia",
    "spain", "sweden", "switzerland", "taiwan", "turkey", "türkiye",
    "uk", "u k", "united kingdom", "usa", "u s a", "us", "u s",
    "united states", "united states of america",
}
_US_STATE_CODES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi",
    "id", "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi",
    "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc",
    "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut",
    "vt", "va", "wa", "wv", "wi", "wy", "dc",
}

# Common institutional words used only to avoid selecting an obvious typo as
# the canonical display spelling. We never rewrite a token from this list.
_INSTITUTION_TERMS = {
    "university", "institute", "technology", "technological", "technical",
    "science", "sciences", "college", "school", "research", "center",
    "centre", "laboratory", "laboratories", "polytechnic", "academy",
}
_CONNECTOR_WORDS = {
    "of", "de", "des", "du", "for", "fur", "für", "di", "della", "van",
    "von", "der", "la", "le", "at", "in", "and",
}


def _clean(value: Any) -> str:
    return "" if value is None else re.sub(r"\s+", " ", str(value).strip())


def affiliation_key(value: Any) -> str:
    """Comparison key that ignores accents, punctuation, case and spacing."""
    s = unicodedata.normalize("NFKD", _clean(value))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    tokens = []
    for token in s.split():
        tokens.append(_TOKEN_EXPANSIONS.get(token, token))
    return " ".join(tokens)


def _parts(value: Any) -> list[str]:
    """Split affiliation components and remove repeated components in-place."""
    raw_parts = [
        _clean(part).strip(" .,:;-–—")
        for part in _SPLIT_RE.split(_clean(value))
    ]
    out: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        key = affiliation_key(part)
        if part and key and key not in seen:
            seen.add(key)
            out.append(part)
    return out


def clean_affiliation_label(value: Any) -> str:
    """Clean formatting inside one label without changing its meaning."""
    parts = _parts(value)
    return ", ".join(parts)


def _location_only_suffix(parts: list[str]) -> bool:
    """True only when every extra component plausibly looks geographical.

    A country/state at the end is not enough on its own: an affiliation such
    as ``University X, École polytechnique ..., Brussels, Belgium`` contains a
    country but also a real organizational unit that must not be discarded.
    """
    if not parts:
        return False

    joined = " ".join(parts)
    if _ORG_UNIT_RE.search(joined):
        return False

    keys = [affiliation_key(part) for part in parts]
    has_definite_location = False
    for part, key in zip(parts, keys):
        if key in _LOCATION_MARKERS or key in _US_STATE_CODES:
            has_definite_location = True
            continue
        if re.search(r"\d", part):
            has_definite_location = True
            continue

        # Unknown city/campus names are allowed only when they look short and
        # location-like. Longer phrases or connector-heavy phrases are too
        # likely to be a school/department/research unit, so we keep them.
        words = key.split()
        if not words or len(words) > 3:
            return False
        if any(word in _CONNECTOR_WORDS for word in words):
            return False

    return has_definite_location


def _similarity(a: str, b: str) -> tuple[float, float]:
    """Return (character_ratio, token_sort_ratio) on a 0..1 scale."""
    if _rapidfuzz is not None:
        return (
            _rapidfuzz.ratio(a, b) / 100.0,
            _rapidfuzz.token_sort_ratio(a, b) / 100.0,
        )
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    token_ratio = difflib.SequenceMatcher(
        None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))
    ).ratio()
    return ratio, token_ratio


def _match_reason(a: str, b: str) -> str | None:
    """Return the high-confidence rule that makes two labels equivalent."""
    ka, kb = affiliation_key(a), affiliation_key(b)
    if not ka or not kb:
        return None
    if ka == kb:
        return "normalized-exact"

    # Abbreviation expansion plus a harmless connector difference, e.g.
    # "TU Darmstadt" vs "Technical University of Darmstadt". Require an
    # actual known abbreviation on at least one side before ignoring "of".
    compact_a = " ".join(t for t in ka.split() if t != "of")
    compact_b = " ".join(t for t in kb.split() if t != "of")
    if compact_a == compact_b and (_abbreviation_count(a) or _abbreviation_count(b)):
        return "abbreviation-alias"

    # Brand/acronym prefix: "ZHAW Zurich University of Applied Sciences" and
    # "Zurich University of Applied Sciences". This is intentionally narrow:
    # only one leading all-caps token may differ and the shared name must have
    # at least three tokens.
    def branded_prefix(longer_raw: str, longer_key: str, shorter_key: str) -> bool:
        key_tokens = longer_key.split()
        short_tokens = shorter_key.split()
        raw_first = re.split(r"[^A-Za-z0-9]+", _clean(longer_raw), maxsplit=1)[0]
        return (
            2 <= len(raw_first) <= 6
            and raw_first.isupper()
            and len(short_tokens) >= 3
            and key_tokens[1:] == short_tokens
        )

    if branded_prefix(a, ka, kb) or branded_prefix(b, kb, ka):
        return "branding-prefix"

    pa = _parts(a)
    pb = _parts(b)
    pka = [affiliation_key(part) for part in pa]
    pkb = [affiliation_key(part) for part in pb]

    # Collapse an organization plus a location suffix, e.g.
    # "University X" and "University X, Boston, MA, USA". We do not collapse
    # organization-unit suffixes such as "University X, School of Computing".
    if len(pka) < len(pkb) and pkb[: len(pka)] == pka:
        if _location_only_suffix(pb[len(pa) :]):
            return "location-suffix"
    if len(pkb) < len(pka) and pka[: len(pkb)] == pkb:
        if _location_only_suffix(pa[len(pb) :]):
            return "location-suffix"

    # Very close spelling variants. We deliberately require near-identical
    # token sets and lengths so organizationally meaningful suffixes survive.
    ta, tb = ka.split(), kb.split()
    if min(len(ta), len(tb)) < 2 or abs(len(ta) - len(tb)) > 1:
        return None
    sa, sb = set(ta), set(tb)
    overlap = len(sa & sb) / max(len(sa | sb), 1)
    ratio, token_ratio = _similarity(ka, kb)
    # Same-length labels tolerate one small typo even when set overlap drops
    # because the misspelled token is technically a different set member.
    if len(ta) == len(tb) and ratio >= 0.98 and token_ratio >= 0.98:
        return "fuzzy-near-exact"
    if overlap >= 0.86 and ratio >= 0.965 and token_ratio >= 0.97:
        return "fuzzy-near-exact"

    # Conservative truncation typo: all tokens align except one, and the
    # shorter differing token is a long prefix of the other. This catches
    # variants such as "Pennsylva" vs "Pennsylvania" without treating
    # unrelated near-spellings (e.g. Stanford/Stamford) as equivalent.
    if len(ta) == len(tb):
        mismatches = [(x, y) for x, y in zip(ta, tb) if x != y]
        if len(mismatches) == 1:
            x, y = mismatches[0]
            shorter, longer = sorted((x, y), key=len)
            if (
                len(shorter) >= 5
                and len(longer) - len(shorter) <= 4
                and longer.startswith(shorter)
            ):
                return "fuzzy-prefix-typo"
    return None


def affiliation_equivalence_reason(a: str, b: str) -> str | None:
    """Return the conservative rule that makes two affiliation labels equivalent.

    This public wrapper exposes the same high-confidence equivalence logic used
    by the display cleaner so other read-only audits can reuse it without
    depending on a private helper. It does not rewrite either input.
    """
    return _match_reason(a, b)


def _same_affiliation(a: str, b: str) -> bool:
    return affiliation_equivalence_reason(a, b) is not None


def _abbreviation_count(value: str) -> int:
    raw = unicodedata.normalize("NFKD", _clean(value))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()
    tokens = re.findall(r"[a-z0-9]+", raw)
    return sum(1 for token in tokens if token in _TOKEN_EXPANSIONS)


def _accent_count(value: str) -> int:
    count = 0
    for ch in _clean(value):
        decomposed = unicodedata.normalize("NFKD", ch)
        if any(unicodedata.combining(piece) for piece in decomposed[1:]):
            count += 1
    return count


def _connector_case_penalty(value: str) -> int:
    tokens = re.findall(r"[^\W\d_]+", _clean(value), flags=re.UNICODE)
    penalty = 0
    for idx, token in enumerate(tokens):
        if idx and token.casefold() in _CONNECTOR_WORDS and token != token.casefold():
            penalty += 1
    return penalty


def _institution_typo_penalty(value: str) -> int:
    """Penalize obvious misspellings of common organization words only."""
    raw = unicodedata.normalize("NFKD", _clean(value))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()
    tokens = re.findall(r"[a-z0-9]+", raw)
    penalty = 0
    for token in tokens:
        if token in _INSTITUTION_TERMS or token in _TOKEN_EXPANSIONS:
            continue
        if len(token) < 6:
            continue
        best = max((_similarity(token, term)[0] for term in _INSTITUTION_TERMS), default=0.0)
        if best >= 0.90:
            penalty += 1
    return penalty


def _location_extension_penalty(value: str, group: list[str]) -> int:
    value_parts = _parts(value)
    penalty = 0
    for other in group:
        if other == value:
            continue
        if _match_reason(value, other) != "location-suffix":
            continue
        other_parts = _parts(other)
        if len(value_parts) > len(other_parts):
            penalty += len(value_parts) - len(other_parts)
    return penalty


def _trim_location_variant(value: str, group: list[str]) -> str:
    """Remove only the location suffix proven redundant by another variant."""
    parts = _parts(value)
    trim_to = len(parts)
    for other in group:
        if other == value or _match_reason(value, other) != "location-suffix":
            continue
        other_parts = _parts(other)
        if len(parts) > len(other_parts):
            trim_to = min(trim_to, len(other_parts))
    return ", ".join(parts[:trim_to])


def _display_choice(group: list[str]) -> str:
    """Choose the most polished display spelling after equivalence is known.

    A candidate may be shortened only by a location suffix already proven
    redundant by another observed variant. Otherwise the cleaner does not
    invent or rewrite institution names.
    """
    candidates = sorted(
        {_trim_location_variant(value, group) for value in group},
        key=str.casefold,
    )
    return min(
        candidates,
        key=lambda x: (
            _institution_typo_penalty(x),
            _abbreviation_count(x),
            _connector_case_penalty(x),
            1 if re.search(r"\([^)]{2,}\)", x) else 0,
            -_accent_count(x),
            -sum(ch in "-–—" for ch in x),
            -len(x),
            x.casefold(),
            x,
        ),
    )


def clean_affiliations_with_audit(
    values: Iterable[Any],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Return display labels plus an inspectable raw->display mapping.

    Audit entries are emitted only for labels whose formatting changed or that
    were merged into another label. The caller can therefore count/report real
    effects without comparing internal normalization keys.
    """
    raw_values = sorted({_clean(v) for v in values if _clean(v)}, key=str.casefold)
    cleaned_by_raw = {raw: clean_affiliation_label(raw) for raw in raw_values}
    labels = sorted(set(cleaned_by_raw.values()), key=str.casefold)

    groups: list[list[str]] = []
    group_reasons: list[set[str]] = []
    for label in labels:
        for idx, group in enumerate(groups):
            reasons = [
                _match_reason(label, existing)
                for existing in group
            ]
            reasons = [reason for reason in reasons if reason]
            if reasons:
                group.append(label)
                group_reasons[idx].update(reasons)
                break
        else:
            groups.append([label])
            group_reasons.append(set())

    display_for_cleaned: dict[str, str] = {}
    merge_reason_for_cleaned: dict[str, str] = {}
    display_values: list[str] = []
    for group, reasons in zip(groups, group_reasons):
        display = _display_choice(group)
        display_values.append(display)
        for label in group:
            display_for_cleaned[label] = display
            if label != display:
                merge_reason_for_cleaned[label] = "+".join(sorted(reasons)) or "duplicate"

    audit: list[dict[str, Any]] = []
    for raw in raw_values:
        cleaned = cleaned_by_raw[raw]
        display = display_for_cleaned[cleaned]
        reasons: list[str] = []
        if raw != cleaned:
            reasons.append("label-formatting")
        if cleaned != display:
            reasons.append(merge_reason_for_cleaned.get(cleaned, "duplicate"))
        if reasons:
            audit.append(
                {
                    "raw": raw,
                    "cleaned": cleaned,
                    "display": display,
                    "reason": "+".join(reasons),
                }
            )

    return sorted(set(display_values), key=str.casefold), audit


def clean_affiliations(values: Iterable[Any]) -> list[str]:
    """Return stable website labels while leaving input/raw data untouched."""
    display, _audit = clean_affiliations_with_audit(values)
    return display


def fuzzy_backend() -> str:
    """Human-readable matcher backend for audit metadata."""
    return "rapidfuzz" if _rapidfuzz is not None else "difflib-fallback"
