"""Shared source versions for every pipeline stage; reading this file is offline."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

MANIFEST_FILENAME = "snapshot_sources.json"
MANIFEST_ENV = "DBML_SNAPSHOT_MANIFEST"
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "data" / MANIFEST_FILENAME

# Existing datasets remain reproducible until the first online source refresh.
LEGACY_SOURCES = {
    "dblp": {
        "id": "dblp-2026-09-01", "filename": "dblp-2026-09-01.xml.gz",
        "url": "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/2026/dblp-2026-09-01.xml.gz",
        "md5": "41abb87b4e8b8a2e3c470efd6528ad88",
    },
    "dtd": {
        "filename": "dblp-2023-06-28.dtd",
        "url": "https://drops.dagstuhl.de/storage/artifacts/dblp/xml/2023/dblp-2023-06-28.dtd",
        "md5": "96e4283e71886dec2173718957293648",
    },
    "ror": {
        "release": "v2.13-2026-09-22", "record": "22902037",
        "filename": "v2.13-2026-09-22-ror-data.zip",
        "url": "https://zenodo.org/records/22902037/files/v2.13-2026-09-22-ror-data.zip?download=1",
        "md5": "a935217de21e7983b977f5419f1a6b20",
    },
}


def validate_sources(sources: dict) -> dict:
    """Require dated monthly artifacts and published checksums, never a daily URL."""
    for key in ("dblp", "dtd", "ror"):
        item = sources.get(key, {})
        filename = item.get("filename", "")
        if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            raise RuntimeError(f"Invalid {key} snapshot filename")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", item.get("md5", "")):
            raise RuntimeError(f"Missing or invalid {key} snapshot MD5")
        url = urlparse(item.get("url", ""))
        hosts = {"zenodo.org"} if key == "ror" else {"drops.dagstuhl.de"}
        if url.scheme != "https" or url.hostname not in hosts:
            raise RuntimeError(f"Unexpected {key} snapshot URL: {item.get('url')}")
    if not re.fullmatch(r"dblp-\d{4}-\d{2}-\d{2}", sources["dblp"].get("id", "")):
        raise RuntimeError("Expected a dated DBLP monthly snapshot")
    if sources["dblp"]["filename"] != sources["dblp"]["id"] + ".xml.gz":
        raise RuntimeError("DBLP snapshot ID and filename disagree")
    if not re.fullmatch(r"v2\.\d+(?:\.\d+)?-\d{4}-\d{2}-\d{2}", sources["ror"].get("release", "")):
        raise RuntimeError("Unsupported ROR release; this pipeline requires the ROR v2 data format")
    if not str(sources["ror"].get("record", "")).isdigit():
        raise RuntimeError("Invalid ROR Zenodo record ID")
    return sources


def load_snapshot_sources(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get(MANIFEST_ENV, DEFAULT_MANIFEST))
    if not path.exists():
        return copy.deepcopy(LEGACY_SOURCES)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise RuntimeError(f"Unsupported snapshot manifest: {path}")
    return validate_sources(payload["sources"])
