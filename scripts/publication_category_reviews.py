"""Persistent paper-level review history, separate from effective category mappings."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

try:
    from .publication_categories import SIDE_CATEGORIES, normalized
except ImportError:
    from publication_categories import SIDE_CATEGORIES, normalized

REVIEWS_PATH = Path(__file__).resolve().parents[1] / 'data/publication_category_reviews.csv'
IDENTITY_FIELDS = ('title', 'year', 'booktitle', 'journal', 'record_type')
REVIEW_FIELDS = ('dblp_key', *IDENTITY_FIELDS, 'category', 'status', 'evidence_url',
                 'source_title', 'verification_method', 'reason', 'reviewed_on', 'snapshot_id')


def load_category_reviews(path: Path = REVIEWS_PATH) -> dict[str, dict[str, str]]:
    """Keep unresolved investigations too; a missing optional ledger is empty."""
    if not path.exists():
        return {}
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source)
        if set(reader.fieldnames or ()) != set(REVIEW_FIELDS):
            raise ValueError(f'{path.name}: unexpected review columns')
        for line, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f'{path.name}:{line}: malformed review row')
            row = {key: value.strip() for key, value in row.items()}
            key = row['dblp_key']
            if not key or key in result:
                raise ValueError(f'{path.name}:{line}: missing or duplicate publication key')
            if row['status'] not in {'confirmed', 'pending'} or row['category'] not in SIDE_CATEGORIES:
                raise ValueError(f'{path.name}:{line}: invalid review status or category')
            if row['verification_method'] not in {'live_primary_source', 'pinned_snapshot_metadata'}:
                raise ValueError(f'{path.name}:{line}: invalid verification method')
            url = urlparse(row['evidence_url'])
            if url.scheme != 'https' or not url.netloc:
                raise ValueError(f'{path.name}:{line}: HTTPS evidence URL required')
            if not all(row[field] for field in ('title', 'record_type', 'source_title', 'reason', 'snapshot_id')):
                raise ValueError(f'{path.name}:{line}: review evidence and identity fields required')
            date.fromisoformat(row['reviewed_on'])
            if not row['year'].isdecimal():
                raise ValueError(f'{path.name}:{line}: publication year required')
            result[key] = row
    return result


def annotate_category_reviews(rows: list[dict], reviews: dict[str, dict]) -> None:
    """A confirmed review clears a flag only while its metadata/category still match."""
    for row in rows:
        review = reviews.get(row['dblp_key'])
        row.update(manual_review_status='', reviewed_on='', review_evidence_url='', review_reason='')
        if not review:
            continue
        row.update(manual_review_status=review['status'], reviewed_on=review['reviewed_on'],
                   review_evidence_url=review['evidence_url'], review_reason=review['reason'])
        if any(normalized(str(row.get(field, ''))) != normalized(review[field]) for field in IDENTITY_FIELDS):
            row['status'] = 'review_metadata_changed'
        elif review['status'] == 'pending':
            row['status'] = 'review_pending'
        elif row['category'] != review['category']:
            row['status'] = 'review_assignment_conflict'
        else:
            # Different versions can correctly retain different categories.
            row['status'] = 'reviewed_assignment'
