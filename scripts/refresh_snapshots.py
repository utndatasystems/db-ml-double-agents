#!/usr/bin/env python3
"""Discover and verify the latest monthly DBLP dump, its DTD and the latest ROR dump."""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from lxml import html
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from .snapshot_sources import DEFAULT_MANIFEST, MANIFEST_FILENAME, validate_sources
except ImportError:
    from snapshot_sources import DEFAULT_MANIFEST, MANIFEST_FILENAME, validate_sources

DBLP_COLLECTION = "https://drops.dagstuhl.de/entities/collection/10.4230/dblp.xml"
ROR_LATEST = "https://zenodo.org/api/records/6347574"
ARTIFACT_BASE = "https://drops.dagstuhl.de/entities/artifact/10.4230/"


def get(session, url):
    response = session.get(url, timeout=(30, 60))
    response.raise_for_status()
    return response


def links(response) -> list[str]:
    return [urljoin(response.url, href) for href in html.fromstring(response.content).xpath("//a/@href")]


def monthly_release_dates(urls: list[str], today: date | None = None) -> list[str]:
    result = set()
    for url in urls:
        match = re.search(r"/10\.4230/dblp\.xml\.(\d{4}-\d{2}-\d{2})$", url)
        if match:
            try:
                value = date.fromisoformat(match[1])
            except ValueError:
                continue
            if value <= (today or date.today()):
                result.add(value.isoformat())
    return sorted(result)


def artifact_file(session, page, suffix: str) -> dict:
    urls = sorted({url for url in links(page)
                   if urlparse(url).hostname == "drops.dagstuhl.de"
                   and re.fullmatch(r"/storage/artifacts/dblp/xml/\d{4}/dblp-\d{4}-\d{2}-\d{2}" + re.escape(suffix), urlparse(url).path)})
    if len(urls) != 1:
        raise RuntimeError(f"Expected one {suffix} download on {page.url}; found {urls}")
    url = urls[0]
    checksum = get(session, url + ".md5").text.strip()
    match = re.search(r"\b([a-fA-F0-9]{32})\b", checksum)
    if not match:
        raise RuntimeError(f"Missing published MD5 for {url}")
    return {"filename": Path(urlparse(url).path).name, "url": url, "md5": match[1].lower(), "sourcePage": page.url}


def discover_dblp(session) -> tuple[dict, dict]:
    dates = monthly_release_dates(links(get(session, DBLP_COLLECTION)))
    if not dates:
        raise RuntimeError("No dated monthly DBLP releases found; source discovery stopped")
    release = dates[-1]
    page = get(session, ARTIFACT_BASE + "dblp.xml." + release)
    snapshot = artifact_file(session, page, ".xml.gz")
    snapshot["id"] = "dblp-" + release
    # Use the schema referenced by this monthly release, not an unrelated latest DTD.
    schema_dates = {match[1] for url in links(page)
                    if (match := re.search(r"/10\.4230/dblp\.xml\.dtd\.(\d{4}-\d{2}-\d{2})$", url))}
    if len(schema_dates) != 1:
        raise RuntimeError("Monthly DBLP release has no unambiguous DTD reference")
    schema_page = get(session, ARTIFACT_BASE + "dblp.xml.dtd." + schema_dates.pop())
    return snapshot, artifact_file(session, schema_page, ".dtd")


def discover_ror(session) -> dict:
    payload = get(session, ROR_LATEST).json()
    if str(payload.get("conceptrecid")) != "6347574":
        raise RuntimeError("Zenodo response is not the ROR data collection")
    files = [item for item in payload.get("files", []) if item.get("key", "").endswith("-ror-data.zip")]
    if len(files) != 1:
        raise RuntimeError("Latest ROR record must contain exactly one data ZIP")
    item = files[0]
    filename = item["key"]
    checksum = item.get("checksum", "")
    if not checksum.startswith("md5:"):
        raise RuntimeError("Latest ROR record has no published MD5")
    return {"release": filename.removesuffix("-ror-data.zip"), "record": str(payload["id"]),
            "filename": filename, "md5": checksum[4:].lower(), "url": item["links"]["self"],
            "sourcePage": f"https://zenodo.org/records/{payload['id']}"}


def discover_sources(session) -> dict:
    dblp, dtd = discover_dblp(session)
    return validate_sources({"dblp": dblp, "dtd": dtd, "ror": discover_ror(session)})


def activate_sources(data_dir: Path, sources: dict, *, force_verify=False) -> None:
    """Publish the selection only after all three downloads pass their checksums."""
    try:
        from .enrich_reviewers_with_dblp import download_file
    except ImportError:
        from enrich_reviewers_with_dblp import download_file
    validate_sources(sources)
    for key, folder in (("dtd", "dblp_snapshot"), ("dblp", "dblp_snapshot"), ("ror", "ror_snapshot")):
        item = sources[key]
        download_file(item["url"], data_dir / folder / item["filename"], item["md5"],
                      f"{key.upper()} snapshot", force_verify=force_verify)
    target = data_dir / MANIFEST_FILENAME
    payload = {"schemaVersion": 1, "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sources": sources}
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
    print(f"Selected snapshots recorded in {target}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_MANIFEST.parent)
    parser.add_argument("--check-only", action="store_true", help="Discover releases without downloading snapshots or changing data")
    parser.add_argument("--verify-snapshots", action="store_true")
    args = parser.parse_args(argv)
    with requests.Session() as session:
        session.headers.update({"User-Agent": "DB-ML-Snapshot-Refresh/1.0", "Accept": "*/*"})
        session.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))))
        sources = discover_sources(session)
    print(f"Latest DBLP monthly snapshot: {sources['dblp']['id']}", flush=True)
    print(f"Referenced DBLP DTD: {sources['dtd']['filename']}", flush=True)
    print(f"Latest ROR release: {sources['ror']['release']}", flush=True)
    if not args.check_only:
        activate_sources(args.data_dir.resolve(), sources, force_verify=args.verify_snapshots)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"Snapshot refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
