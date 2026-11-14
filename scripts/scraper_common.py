#!/usr/bin/env python3
"""Small shared primitives for conference service scrapers.

Keep this module intentionally boring: text cleanup, conservative name keys,
and browser-visible line extraction. Venue-specific section/role logic belongs
in each conference scraper.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup


def clean_line(text: str) -> str:
    text = str(text or "").replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n")


def normalize_name_key(name: str) -> str:
    """Conservative blocking key for candidate generation, not identity."""
    value = unicodedata.normalize("NFKD", str(name or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_visible_lines(html: bytes) -> list[str]:
    """Return browser-like text rows while preserving explicit <br> breaks."""
    soup = BeautifulSoup(html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    return [
        line
        for raw in soup.get_text("\n").splitlines()
        if (line := clean_line(raw))
    ]
