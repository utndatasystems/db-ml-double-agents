"""Automatic author-specific affiliation evidence from publisher DOI metadata.

Only known DBLP publications participate. Exact ORCIDs or unique full names
identify the author; metadata update timestamps never establish affiliation dates.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import unicodedata
import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

import requests

POLICY_VERSION = 3
OUTPUT_FILENAME = "automatic_affiliation_evidence.json"
USER_AGENT = "db-ml-affiliation-evidence/1.0"


def normalized(value: str) -> str:
    value = re.sub(r"\s+\d{4}$", "", html.unescape(value or ""))
    value = re.sub(r"<[^>]+>", "", value)
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def doi_from_url(value: str) -> str:
    match = re.search(r"(?:doi\.org/|^)(10\.\d{4,9}/\S+)", unquote(value or ""), re.I)
    return match.group(1).rstrip(".,;").lower() if match else ""


def publication_dois(paper: dict) -> list[str]:
    """Use only same-year versions authored by this person.

    Legacy rows without ownership metadata prove only the selected record.
    An explicit empty ownership list proves none of the merged versions.
    """
    owned = set(paper.get("personVersionDblpKeys", [paper.get("dblpKey")]))
    values = [paper.get("url", "")] if paper.get("dblpKey") in owned else []
    for version in paper.get("resolvedVersions") or []:
        if version.get("dblpKey") not in owned:
            continue
        if version.get("year") and version["year"] != paper.get("year"):
            continue
        values.extend([version.get("url", ""), *(version.get("ees") or [])])
    return sorted({doi_from_url(v) for v in values if doi_from_url(v) and not doi_from_url(v).startswith("10.48550/")})


class MetadataCache:
    """Persistent positive/negative cache; public Crossref pool: one request at a time."""
    def __init__(self, path: Path, *, offline=False, refresh_days=30, refresh=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS responses (url TEXT PRIMARY KEY, fetched_at TEXT, status INTEGER, payload TEXT)")
        self.offline = offline
        self.refresh_days = refresh_days
        self.refresh = refresh
        self.refreshed_urls = set()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.next_request = 0.0
        self.requests = 0
        self.errors = 0

    def close(self):
        self.session.close()
        self.conn.close()

    def prefetch(self, dois: list[str], *, batch_size=40):
        """Batch exact DOI filters (OR semantics), caching each returned record.

        Missing results are negative-cached only for complete successful batches.
        A list request is limited to once per second in Crossref's public pool.
        """
        if self.offline:
            return
        missing = []
        for doi in sorted(set(dois)):
            url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
            row = self.conn.execute("SELECT fetched_at,status FROM responses WHERE url=?", (url,)).fetchone()
            ttl = self.refresh_days if row and row[1] == 200 else 1
            if self.refresh or not row or datetime.now(timezone.utc) - datetime.fromisoformat(row[0]) >= timedelta(days=ttl):
                missing.append(doi)
        for start in range(0, len(missing), batch_size):
            group = missing[start:start + batch_size]
            params = {"filter": ",".join(f"doi:{doi}" for doi in group), "rows": len(group),
                      "select": "DOI,title,author,published-online,published-print,published,issued"}
            url = "https://api.crossref.org/works?" + urlencode(params)
            payload, stamp = self.get(url)
            self.next_request = time.monotonic() + 1.05
            if not payload or not isinstance(payload.get("message"), dict):
                continue
            message = payload["message"]
            items = {item.get("DOI", "").lower(): item for item in message.get("items", [])}
            complete = message.get("total-results", len(items) + 1) <= len(items)
            for doi in group:
                if doi not in items and not complete:
                    continue
                single_url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
                record = {"message": items[doi]} if doi in items else None
                self.conn.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)", (single_url, stamp, 200 if record else 404, json.dumps(record, ensure_ascii=False)))
                self.refreshed_urls.add(single_url)
            self.conn.commit()
            print(f"Affiliation metadata: {min(start + batch_size, len(missing))}/{len(missing)} DOI records refreshed", flush=True)

    def get(self, url: str) -> tuple[dict | None, str]:
        cached = self.conn.execute("SELECT fetched_at,status,payload FROM responses WHERE url=?", (url,)).fetchone()
        now = datetime.now(timezone.utc)
        if cached:
            ttl = self.refresh_days if cached[1] == 200 else 1
            fresh = now - datetime.fromisoformat(cached[0]) < timedelta(days=ttl)
            if self.offline or url in self.refreshed_urls or (fresh and not self.refresh):
                return (json.loads(cached[2]) if cached[1] == 200 else None), cached[0]
        if self.offline:
            return None, ""
        for attempt in range(3):
            time.sleep(max(0, self.next_request - time.monotonic()))
            try:
                response = self.session.get(url, timeout=(10, 20))
                self.requests += 1
                self.next_request = time.monotonic() + .22
                if response.status_code == 429 or response.status_code >= 500:
                    self.next_request = time.monotonic() + min(30, float(response.headers.get("Retry-After", 2 ** (attempt + 1))))
                    continue
                status = response.status_code
                payload = response.json() if status == 200 else None
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.conn.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?)", (url, stamp, status, json.dumps(payload, ensure_ascii=False)))
                self.conn.commit()
                self.refreshed_urls.add(url)
                return payload, stamp
            except (requests.RequestException, ValueError):
                self.next_request = time.monotonic() + 2 ** attempt
        self.errors += 1
        self.refreshed_urls.add(url)
        # Preserve the previous successful snapshot during an outage and disclose its age.
        if cached and cached[1] == 200:
            return json.loads(cached[2]), cached[0]
        return None, ""


def matched_author(authors: list[dict], names: list[str], orcids: list[str]) -> tuple[dict | None, str]:
    known_ids = {value.rsplit("/", 1)[-1].upper() for value in orcids}
    exact_ids = [a for a in authors if a.get("ORCID", "").rsplit("/", 1)[-1].upper() in known_ids]
    if len(exact_ids) == 1:
        return exact_ids[0], "exact_orcid"
    if exact_ids:
        return None, "ambiguous_orcid"
    known_names = {normalized(n) for n in names if normalized(n)}
    matches = [a for a in authors if normalized(f"{a.get('given', '')} {a.get('family', '')}") in known_names]
    if len(matches) != 1:
        return None, "ambiguous_or_missing_full_name"
    candidate = matches[0]
    if known_ids and candidate.get("ORCID") and candidate["ORCID"].rsplit("/", 1)[-1].upper() not in known_ids:
        return None, "conflicting_orcid"
    return candidate, "exact_full_name"


def published_date(message: dict, today: date | None = None) -> tuple[str, int] | None:
    today = today or date.today()
    for field in ["published-online", "published", "published-print", "issued"]:
        parts = (message.get(field, {}).get("date-parts") or [[]])[0]
        if not parts or not 1900 <= parts[0] <= today.year:
            continue
        try:
            value = date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
        except (TypeError, ValueError):
            continue
        if value <= today:
            return value.isoformat()[:[0, 4, 7, 10][min(len(parts), 3)]], len(parts)
    return None


def crossref_evidence(message: dict, doi: str, paper: dict, names: list[str], orcids: list[str], fetched_at: str) -> tuple[dict | None, str]:
    if message.get("DOI", "").lower() != doi.lower():
        return None, "doi_mismatch"
    titles = message.get("title") or []
    if not titles or normalized(titles[0]) != normalized(paper.get("title", "")):
        return None, "title_mismatch"
    author, match = matched_author(message.get("author") or [], names, orcids)
    if author is None:
        return None, match
    affiliations = sorted({html.unescape(a.get("name", "")).strip() for a in author.get("affiliation", []) if a.get("name", "").strip()})
    if not affiliations:
        return None, "no_author_affiliation"
    published = published_date(message)
    if not published:
        return None, "missing_or_future_publication_date"
    # A deposit/index update is not evidence of a recent publication.
    if abs(int(published[0][:4]) - int(paper.get("year") or 0)) > 1:
        return None, "publication_year_mismatch"
    return {
        "dblpKey": paper["dblpKey"], "year": paper["year"],
        "sourceDate": published[0], "datePrecision": published[1],
        "rawAffiliations": affiliations, "authorMatch": match,
        "sourceUrl": f"https://doi.org/{doi}",
        "metadataUrl": f"https://api.crossref.org/works/{quote(doi, safe='')}",
        "retrievedAt": fetched_at, "provider": "Crossref publisher metadata",
    }, "matched"


def evidence_refreshed_at(snapshot: dict) -> str:
    """Use successful metadata retrievals, never a later cache-only build time."""
    stamps = [snapshot.get("refreshedAt")]
    for record in snapshot.get("people", {}).values():
        stamps.extend(item.get("retrievedAt") for key in ("candidates", "checks") for item in record.get(key, []))
    dates = []
    for stamp in stamps:
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except (TypeError, ValueError, AttributeError):
            continue
        if parsed.tzinfo is not None:
            dates.append(parsed.astimezone(timezone.utc))
    return max(dates).isoformat(timespec="seconds") if dates else ""


def collect_evidence(person: dict, papers: list[dict], client: MetadataCache, *, max_papers=0) -> dict:
    names = [person.get("name", ""), *person.get("aliases", {}).get("db", []), *person.get("aliases", {}).get("publication", [])]
    latest_year = max((int(p.get("year") or 0) for p in papers), default=0)
    candidates = sorted([p for p in papers if p.get("year") == latest_year and publication_dois(p)], key=lambda p: p["dblpKey"])
    evidence, outcomes, seen = [], [], set()
    chosen = candidates[:max_papers] if max_papers else candidates
    for paper in chosen:
        for doi in publication_dois(paper):
            if doi in seen:
                continue
            seen.add(doi)
            url = f"https://api.crossref.org/works/{quote(doi, safe='')}"
            payload, fetched_at = client.get(url)
            if payload:
                item, outcome = crossref_evidence(payload.get("message", {}), doi, paper, [*names, paper.get("localAuthorName", "")], person.get("orcids", []), fetched_at)
                if item:
                    evidence.append(item)
            else:
                outcome = "metadata_unavailable"
            outcomes.append({"dblpKey": paper["dblpKey"], "doi": doi, "outcome": outcome,
                             "retrievedAt": fetched_at if payload else ""})
    return {"name": person["name"], "latestPublicationYear": latest_year, "candidates": evidence, "checks": outcomes,
            "papersConsidered": len(chosen), "latestYearDoiPapers": len(candidates), "searchCapped": bool(max_papers and len(candidates) > max_papers)}


def date_interval(value: str) -> tuple[date, date]:
    parts = [int(p) for p in value.split("-")]
    start = date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    if len(parts) == 1:
        end = date(parts[0], 12, 31)
    elif len(parts) == 2:
        end = date(parts[0], parts[1], calendar.monthrange(parts[0], parts[1])[1])
    else:
        end = start
    return start, end


def organization(raw: str, matcher=None, known_labels=()) -> tuple[str, str]:
    """Use an exact ROR match's observed surface; preserve ambiguous raw labels."""
    matches = matcher.match_affiliation(raw) if matcher else {}
    if len(matches) == 1:
        ror_id, match = next(iter(matches.items()))
        return ror_id, match.matched_surface
    if len(matches) > 1:
        return "|".join(sorted(matches)), raw
    # The entire existing institution label must occur, with word boundaries.
    # This trims addresses such as 'University of Brescia Via Branze'.
    for label in sorted(known_labels, key=lambda value: (-len(value), value.casefold(), value)):
        if len(label) >= 8 and re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", raw, re.I):
            label_matches = matcher.match_affiliation(label) if matcher else {}
            key = next(iter(label_matches)) if len(label_matches) == 1 else normalized(label)
            return key, label
    first = raw.split(",")[0].strip()
    if not re.search(r"university|institut|college|school|department|faculty|laborator|graduate", first, re.I):
        return normalized(first), first
    return normalized(raw), raw


def select_automatic_affiliation(primary: dict, record: dict, *, matcher=None, known_labels=()) -> dict:
    """Select latest usable dated evidence, retaining uncertainty and source links."""
    result = {**primary, "automaticAffiliationStatus": "no_usable_publication_metadata"}
    evidence = record.get("candidates") or []
    if not evidence:
        return result
    if record.get("searchCapped"):
        return {**result, "automaticAffiliationStatus": "incomplete_search"}
    # A year-only date can overlap a later precise date. Do not pretend it was
    # published on January 1 and silently discard a conflicting institution.
    latest_start = max(date_interval(item["sourceDate"])[0] for item in evidence)
    newest = [item for item in evidence if date_interval(item["sourceDate"])[1] >= latest_start]
    source_year = latest_start.year
    if source_year < (primary.get("latestServiceYear") or 0):
        return {**result, "automaticAffiliationStatus": "publication_older_than_service"}
    groups = []
    for item in newest:
        organizations = dict(organization(raw, matcher, known_labels) for raw in item["rawAffiliations"])
        groups.append((tuple(sorted(organizations)), organizations, item))
    if len({group[0] for group in groups}) != 1:
        return {**result, "automaticAffiliationStatus": "conflicting_latest_publications", "hasConflict": True,
                "confidence": "low", "automaticAffiliationEvidence": newest}
    keys, organizations, _ = groups[0]
    # Same-year service has no month/day. If it disagrees, recency is unknown.
    if source_year == primary.get("latestServiceYear") and primary.get("latestServiceAffiliations"):
        service_keys = {organization(label, matcher, known_labels)[0] for label in primary["latestServiceAffiliations"]}
        if not set(keys).issubset(service_keys):
            return {**result, "automaticAffiliationStatus": "same_year_service_conflict", "hasConflict": True,
                    "confidence": "low", "automaticAffiliationEvidence": newest}
    # Prefer the most precise, latest corroborating source for a short popup.
    chosen = max(newest, key=lambda item: (len(item["sourceDate"]), item["sourceDate"], item["dblpKey"]))
    display = " · ".join(organizations[key] for key in sorted(organizations))
    return {**primary, "affiliation": display, "decisionKey": "automatic_publication_affiliation",
            "confidence": "medium", "hasConflict": len(keys) > 1,
            "automaticAffiliationStatus": "selected", "automaticAffiliationDate": chosen["sourceDate"],
            "automaticAffiliationYear": source_year, "automaticAffiliationEvidence": newest,
            "automaticAffiliationSource": chosen,
            "notes": ["Latest usable author-specific publisher affiliation; a publication affiliation does not prove current employment."]}


def apply_automatic_evidence(primary: dict, record: dict, papers: list[dict], catalog: dict, *, matcher=None, known_labels=()) -> dict:
    indexed = {p["dblpKey"]: p for p in papers}
    # Reject stale, mismatched or tampered evidence rather than attaching it to a different PID.
    valid = []
    for item in record.get("candidates", []):
        paper = indexed.get(item.get("dblpKey"))
        if (paper and paper.get("year") == item.get("year")
                and doi_from_url(item.get("sourceUrl", "")) in publication_dois(paper)):
            valid.append(item)
    result = select_automatic_affiliation(primary, {**record, "candidates": valid}, matcher=matcher, known_labels=known_labels)
    chosen = result.get("automaticAffiliationSource")
    linked_evidence = [chosen] if chosen else result.get("automaticAffiliationEvidence", [])[:2]
    source_ids = []
    for chosen in linked_evidence:
        source_id = "automatic_affiliation:" + hashlib.sha256(f"{chosen['dblpKey']}|{chosen['sourceUrl']}".encode()).hexdigest()[:12]
        catalog["sources"][source_id] = {"kind": "automatic_publication_affiliation", "provider": chosen["provider"],
            "label": f"Publisher affiliation ({chosen['sourceDate']})", "url": chosen["sourceUrl"],
            "metadataUrl": chosen["metadataUrl"], "retrievedAt": chosen["retrievedAt"]}
        source_ids.append(source_id)
    if source_ids:
        result["sourceIds"] = sorted(set([*result.get("sourceIds", []), *source_ids]))
    return result
