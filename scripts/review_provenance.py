"""Expose project review decisions separately from their primary evidence sources."""
from __future__ import annotations

import csv
import hashlib
import json
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

REVIEW_FILES = {
    'reviewed_person_identities.csv': 'identity',
    'reviewed_service_aliases.csv': 'identity',
    'reviewed_publication_authors.csv': 'authorship',
    'publication_category_reviews.csv': 'category',
    'publication_affiliation_evidence.csv': 'affiliation',
}
ATTRIBUTIONS_FILE = 'review_attributions.csv'
METHODS = {'ai_assisted', 'human', 'project_review'}


def csv_rows(path):
    if not path.exists():
        return []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return [{k: (v or '').strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def review_key(filename, row):
    if filename in {'reviewed_person_identities.csv', 'reviewed_service_aliases.csv'}:
        return row['service_name']
    if filename == 'reviewed_publication_authors.csv':
        return json.dumps([row['dblp_key'], row['target_pid'], row['author_signature']], ensure_ascii=False)
    if filename == 'publication_affiliation_evidence.csv':
        return json.dumps([row['dblp_key'], row['dblp_pid'], row['affiliation']], ensure_ascii=False)
    return row['dblp_key']


def decision_fingerprint(row):
    text = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def load_project_reviews(data_dir):
    data_dir = Path(data_dir)
    attributions = {}
    for row in csv_rows(data_dir / ATTRIBUTIONS_FILE):
        key = (row['review_file'], row['review_key'])
        if key in attributions or key[0] not in REVIEW_FILES or row['review_method'] not in METHODS:
            raise ValueError('Invalid or duplicate project review attribution: ' + str(key))
        if len(row['decision_sha256']) != 64 or any(c not in '0123456789abcdef' for c in row['decision_sha256']):
            raise ValueError('Invalid review decision fingerprint: ' + str(key))
        attributions[key] = row
    index = {kind: defaultdict(list) for kind in {'identity', 'authorship', 'category', 'affiliation'}}
    for filename, kind in REVIEW_FILES.items():
        for row in csv_rows(data_dir / filename):
            key = review_key(filename, row)
            origin = attributions.get((filename, key))
            if origin and origin['decision_sha256'] != decision_fingerprint(row):
                raise ValueError(f'Review attribution needs rechecking after a decision changed: {filename}: {key}')
            reviewed_on = row.get('reviewed_on') or row.get('verified_on') or ''
            if reviewed_on:
                date.fromisoformat(reviewed_on)
            urls = json.loads(row['evidence_urls_json']) if row.get('evidence_urls_json') else [row.get('source_url') or row.get('evidence_url')]
            urls = [u for u in urls if u]
            if any(urlsplit(u).scheme != 'https' or not urlsplit(u).netloc for u in urls):
                raise ValueError('Review evidence must use HTTPS: ' + filename)
            review = {
                'kind': kind,
                'method': origin['review_method'] if origin else 'project_review',
                'reviewedOn': reviewed_on,
                'reason': row.get('reason', ''),
                'evidenceUrls': list(dict.fromkeys(urls)),
                'reviewFile': filename,
                'recordKey': key,
                'publicationKey': row.get('dblp_key', ''),
            }
            if kind == 'identity':
                review['serviceName'] = row['service_name']
                index[kind][row['target_pid']].append(review)
            elif kind == 'category':
                review.update(status=row['status'], category=row['category'])
                review['publicationMetadata'] = {
                    field: row.get(column, '') for field, column in (
                        ('title', 'title'), ('year', 'year'), ('booktitle', 'booktitle'),
                        ('journal', 'journal'), ('recordType', 'record_type'),
                    )
                }
                index[kind][row['dblp_key']].append(review)
            else:
                if kind == 'authorship':
                    review['authorSignature'] = row['author_signature']
                else:
                    review['affiliation'] = row['affiliation']
                index[kind][row.get('target_pid') or row['dblp_pid']].append(review)
    return index


def annotate_project_reviews(people, index):
    """Attach decisions only to their actual person, authored versions and category."""
    def name_key(value):
        # Display cleaning can restore accents to a roster spelling. This is
        # exact accent/case normalization, never a fuzzy identity decision.
        text = ''.join(c for c in unicodedata.normalize('NFKD', value)
                       if not unicodedata.combining(c))
        return ' '.join(text.casefold().split())

    for person in people:
        pid = person['dblpPid']
        observed_names = {name_key(n) for n in [*person.get('aliases', {}).get('db', []), person.get('name', '')]}
        identity = [r for r in index['identity'].get(pid, []) if name_key(r['serviceName']) in observed_names]
        if identity:
            person.setdefault('provenance', {}).setdefault('identity', {})['projectReviews'] = identity
        primary = person.get('provenance', {}).get('primaryAffiliation', {})
        evidence = primary.get('publicationAffiliationEvidence', [])
        verified_keys = {item['dblpKey'] for item in evidence}
        selected_keys = {item['dblpKey'] for item in evidence
                         if item['year'] == primary.get('publicationAffiliationYear')}
        affiliation = [{**r, 'usedForDisplay': (
            primary.get('decisionKey') == 'verified_publication_affiliation'
            and r['publicationKey'] in selected_keys
        )} for r in index['affiliation'].get(pid, []) if r['publicationKey'] in verified_keys]
        if affiliation:
            primary['projectReviews'] = affiliation
        for paper in person.get('ml', {}).get('papers', []):
            # A linked version authored only by someone else cannot transfer a review.
            person_keys = set(paper.get('personVersionDblpKeys', [paper.get('dblpKey')]))
            reviews = [{**r, 'personName': person.get('name', '')}
                       for r in index['authorship'].get(pid, []) if r['publicationKey'] in person_keys]
            display_key = paper.get('dblpKey')
            reviews += [r for r in index['category'].get(display_key, [])
                        if r['status'] == 'confirmed' and r['category'] == paper.get('publicationClass')
                        and all(str(paper.get(field, '')).strip() == value
                                for field, value in r['publicationMetadata'].items())]
            if reviews:
                paper.setdefault('provenance', {})['projectReviews'] = reviews
