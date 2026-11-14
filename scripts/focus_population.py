"""A shared, auditable publication-focus cohort, independent of display categories.

Full main-conference DB papers are compared with the site's exact main ML works.
PVLDB/PACMMOD are conference publication channels, not ordinary journal evidence.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import html
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .home_population import PC_ROLES, START_YEAR, END_YEAR, VENUES
    from .enrich_reviewers_with_dblp import SNAPSHOT_FILENAME
except ImportError:
    from home_population import PC_ROLES, START_YEAR, END_YEAR, VENUES
    from enrich_reviewers_with_dblp import SNAPSHOT_FILENAME

ROOT = Path(__file__).resolve().parents[1]
RULES_PATH = ROOT / 'data' / 'focus_venue_rules.csv'
EXCLUSIONS_PATH = ROOT / 'data' / 'focus_publication_exclusions.csv'
POLICY_VERSION = 3
LABELS = ('DB-focused', 'ML-focused', 'Mixed', 'Limited evidence')
RECORD = re.compile(br'<(article|inproceedings|incollection)\b[^>]*\bkey="([^"]+)"')


def rules():
    with RULES_PATH.open(encoding='utf-8', newline='') as f:
        return tuple(csv.DictReader(f))


def exclusions():
    with EXCLUSIONS_PATH.open(encoding='utf-8', newline='') as f:
        result = tuple(csv.DictReader(f))
    reviewed_keys = set()
    for row in result:
        if (row['field'], row['match_type']) not in {
                ('title', 'prefix'), ('title', 'contains'), ('dblp_key', 'exact')}:
            raise ValueError(f"Unsupported focus exclusion: {row['field']} / {row['match_type']}")
        if row['field'] == 'dblp_key' and not all(row.get(k) for k in ('value', 'reason', 'evidence_url', 'verified_on')):
            raise ValueError(f"Reviewed publication exclusion needs evidence and a review date: {row['value']}")
        if row['field'] != 'dblp_key':
            continue
        if row['value'] in reviewed_keys:
            raise ValueError(f"Duplicate or conflicting reviewed publication exclusion: {row['value']}")
        reviewed_keys.add(row['value'])
        try:
            reviewed_on = date.fromisoformat(row['verified_on'])
            if reviewed_on.isoformat() != row['verified_on']:
                raise ValueError('Noncanonical date')
        except ValueError:
            raise ValueError(f"Reviewed publication exclusion needs an ISO review date (YYYY-MM-DD): {row['value']}") from None
        try:
            source = urlsplit(row['evidence_url'])
            valid_source = source.scheme == 'https' and bool(source.hostname) and not any(c.isspace() for c in row['evidence_url'])
        except ValueError:
            valid_source = False
        if not valid_source:
            raise ValueError(f"Reviewed publication exclusion needs an HTTPS evidence URL: {row['value']}")
    return result


def matching_exclusion(paper, excluded_rules):
    """Apply reviewed keys exactly; retain the existing narrow title rules."""
    reviewed = next((row for row in excluded_rules
                     if row['field'] == 'dblp_key' and paper.get('dblpKey') == row['value']), None)
    if reviewed:
        return reviewed
    for excluded in excluded_rules:
        if excluded['field'] == 'dblp_key':
            continue
        title, value = paper.get('title', '').strip().casefold(), excluded['value'].casefold()
        if (excluded['match_type'] == 'prefix' and title.startswith(value)
                or excluded['match_type'] == 'contains' and value in title):
            return excluded
    return None


def matching_rule(paper, venue_rules):
    key = paper.get('dblpKey', '')
    return next((r for r in venue_rules if key.startswith(r['namespace'])
                 and paper.get(r['field'], '').casefold() == r['value'].casefold()), None)


def page_count(pages):
    # Ordinary page ranges and ACM article-number pagination, e.g. 42:1-42:18.
    m = re.fullmatch(r'(?:\d+:)?(\d+)[-\u2013](?:\d+:)?(\d+)', str(pages).strip())
    return int(m[2]) - int(m[1]) + 1 if m and int(m[2]) >= int(m[1]) else None


def db_paper_decision(paper, metadata, venue_rules=None, excluded_rules=None):
    rule = matching_rule(paper, venue_rules if venue_rules is not None else rules())
    if not rule:
        return False, 'Outside the selected DB main proceedings'
    if metadata is None:
        return False, 'Missing snapshot metadata'
    excluded = matching_exclusion(paper, excluded_rules if excluded_rules is not None else exclusions())
    if excluded:
        return False, excluded['reason']
    if metadata.get('publtype', '').lower() in {'informal', 'withdrawn'}:
        return False, 'Informal or withdrawn record'
    count = page_count(metadata.get('pages', ''))
    if count is None:
        if rule['allow_unpaginated'] == 'true' and not metadata.get('pages'):
            return True, 'Unpaginated CIDR main-conference paper'
        return False, 'Full-paper length cannot be verified'
    if count < int(rule['min_pages']):
        return False, 'Short item: fewer than six pages'
    return True, 'Full paper in selected DB main proceedings'


def read_metadata(snapshot, keys, cache):
    """Read exact selected DB records from the existing dump, never download it."""
    fingerprint = [snapshot.name, snapshot.stat().st_size, snapshot.stat().st_mtime_ns]
    if cache.exists():
        saved = json.loads(cache.read_text(encoding='utf-8'))
        if saved.get('snapshot') == fingerprint and keys.issubset(saved['records']):
            return saved['records']
    found, pending = {}, b''
    with gzip.open(snapshot, 'rb') as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            buffer, position = pending + chunk, 0
            while True:
                match = RECORD.search(buffer, position)
                if not match:
                    last = buffer.rfind(b'<', position)
                    pending = buffer[last:] if last >= 0 else buffer[-1024:]
                    break
                key = match[2].decode('utf-8')
                if key not in keys:
                    position = match.end()
                    continue
                closing = b'</' + match[1] + b'>'
                end = buffer.find(closing, match.end())
                if end < 0:
                    pending = buffer[match.start():]
                    break
                block = buffer[match.start():end + len(closing)].decode('utf-8')
                position = end + len(closing)
                item = {}
                for field in ('pages', 'volume', 'number', 'crossref', 'year'):
                    value = re.search(r'<' + field + r'(?:\s[^>]*)?>(.*?)</' + field + '>', block, re.S)
                    item[field] = html.unescape(re.sub('<[^>]+>', '', value[1])).strip() if value else ''
                publtype = re.search(r'\bpubltype="([^"]+)"', match[0].decode('utf-8'))
                item['publtype'] = publtype[1] if publtype else ''
                found[key] = item
    missing = keys - found.keys()
    if missing:
        raise RuntimeError(f'{len(missing)} focus records missing from pinned DBLP snapshot: {sorted(missing)[:3]}')
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix('.tmp')
    temporary.write_text(json.dumps({'snapshot': fingerprint, 'records': found}), encoding='utf-8')
    temporary.replace(cache)
    return found


def qualifying_pc_events(person):
    venues = {v for v, _ in VENUES}
    return [e for e in person.get('db', {}).get('events', [])
            if e.get('venue') in venues and e.get('role') in PC_ROLES
            and isinstance(e.get('year'), int) and START_YEAR <= e['year'] <= END_YEAR
            and 'shadow' not in str(e.get('track', '')).casefold()]


def publication_profile(db_papers, ml_papers, years, *, db_years=(), service_years=()):
    standard_evidence = db_papers + ml_papers >= 5 and len(set(years)) >= 3
    service_evidence = (db_papers >= 2 and db_papers >= 2 * ml_papers
                        and len(set(db_years)) >= 2 and len(set(service_years)) >= 2)
    basis = 'publication_balance'
    if not standard_evidence and service_evidence:
        label = 'DB-focused'
        reason = 'At least twice as many DB as ML conference papers; two DB papers in two publication years plus PC service in two years.'
        basis = 'publication_and_service'
    elif not standard_evidence:
        label = 'Limited evidence'
        reason = 'Neither the five-paper/three-year minimum nor the repeated-PC-service fallback is met.'
        basis = 'insufficient_evidence'
    elif db_papers >= 2 * ml_papers:
        label, reason = 'DB-focused', 'At least twice as many DB as ML conference papers.'
        basis = 'publication_record'
    elif ml_papers >= 2 * db_papers:
        label, reason = 'ML-focused', 'At least twice as many ML as DB conference papers.'
    else:
        label, reason = 'Mixed', 'Neither DB nor ML papers outnumber the other two to one.'
    return {'label': label, 'dbPapers': db_papers, 'mlPapers': ml_papers,
            'publicationYears': len(set(years)), 'dbPublicationYears': len(set(db_years)),
            'serviceYears': len(set(service_years)), 'basis': basis, 'reason': reason}


def apply_focus_population(people, data_dir, snapshot=None):
    venue_rules, excluded_rules = rules(), exclusions()
    def versions(paper):
        return [paper, *paper.get('resolvedVersions', [])]
    def owned_keys(paper):
        # Component membership proves work equivalence, not authorship of every
        # version. Legacy rows establish ownership of their selected record only.
        return set(paper.get('personVersionDblpKeys', [paper['dblpKey']]))
    all_papers = {p['dblpKey']: p for person in people for work in person['ml']['papers']
                  for p in versions(work) if matching_rule(p, venue_rules)}
    snapshot = snapshot or data_dir / 'dblp_snapshot' / SNAPSHOT_FILENAME
    metadata = read_metadata(snapshot, set(all_papers), data_dir / 'reports' / 'focus_snapshot_metadata.json') if all_papers else {}
    decisions = {key: db_paper_decision(p, metadata.get(key), venue_rules, excluded_rules)
                 for key, p in all_papers.items()}
    candidates, included, audit = [], [], []
    for person in people:
        db, ml, years, db_work_years = set(), set(), set(), {}
        for paper in person['ml']['papers']:
            key = paper.get('workId') or paper.get('canonicalDblpKey') or paper['dblpKey']
            authored = owned_keys(paper)
            is_ml = (paper['dblpKey'] in authored and paper.get('scope') == 'main'
                     and paper.get('venue') in {'NeurIPS', 'ICML', 'ICLR'})
            db_versions = [v for v in versions(paper) if v['dblpKey'] in authored
                           and decisions.get(v['dblpKey'], (False, ''))[0]]
            is_db = bool(db_versions)
            if is_db:
                db_work_years.setdefault(key, set()).update(v['year'] for v in db_versions if isinstance(v.get('year'), int))
            if is_ml or is_db:
                (ml if is_ml else db).add(key)
                qualifying_years = [v['year'] for v in db_versions if isinstance(v.get('year'), int)]
                if is_ml and isinstance(paper.get('year'), int):
                    qualifying_years.append(paper['year'])
                if qualifying_years:
                    years.add(min(qualifying_years))
            for version in {v['dblpKey']: v for v in versions(paper) if v['dblpKey'] in decisions}.values():
                vkey = version['dblpKey']
                excluded = matching_exclusion(version, excluded_rules) or {}
                counted, reason = decisions[vkey] if vkey in authored else (False, 'Version not authored by this person')
                audit.append({'pid': person['dblpPid'], 'name': person['name'], 'key': vkey,
                              'work_id': key, 'counted': counted, 'reason': reason,
                              'pages': metadata[vkey].get('pages', ''), 'title': version['title'],
                              'exclusion_evidence_url': excluded.get('evidence_url') or '',
                              'exclusion_verified_on': excluded.get('verified_on') or ''})
        pc = qualifying_pc_events(person)
        db_years = {min(work_years) for work_years in db_work_years.values() if work_years}
        profile = publication_profile(len(db), len(ml), years, db_years=db_years, service_years={e['year'] for e in pc})
        profile['included'] = bool(pc) and profile['label'] == 'DB-focused'
        person['focus'] = profile
        if profile['included']:
            included.append(person)
        if pc:
            candidates.append({'id': person['id'] if profile['included'] else '', 'dblpPid': person['dblpPid'],
                               'name': person['name'], 'primaryAffiliation': person.get('primaryAffiliation', ''),
                               'focus': profile, 'db': {'events': pc}})
    groups = Counter(p['focus']['label'] for p in candidates)
    report_dir = data_dir / 'reports'
    report_dir.mkdir(parents=True, exist_ok=True)
    if audit:
        with (report_dir / 'focus_publication_audit.csv').open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(audit[0])); writer.writeheader(); writer.writerows(audit)
    policy = {'version': POLICY_VERSION, 'candidateCount': len(candidates), 'includedCount': len(included),
              'serviceFallbackCount': sum(p['focus']['basis'] == 'publication_and_service' for p in included),
              'groups': [{'label': label, 'count': groups[label]} for label in LABELS], 'candidates': candidates,
              'definition': 'At least twice as many full DB main-conference papers as main NeurIPS/ICML/ICLR papers. Evidence requires five qualifying papers across three publication years, or two DB papers in two publication years plus qualifying PC service in two distinct years. Qualifying DB PC service in 2022–2025 is required. All recorded publication years are used.',
              'dbPaperPolicy': 'Selected main proceedings only; at least six pages where paginated. Workshops, companion proceedings, known non-paper items and ordinary journals are excluded. PVLDB and PACMMOD count as conference publication channels. Unpaginated CIDR papers qualify.',
              'rulesFingerprint': hashlib.sha256(RULES_PATH.read_bytes() + EXCLUSIONS_PATH.read_bytes()).hexdigest()}
    return included, policy


def cohort_summary(people, previous):
    result = dict(previous)
    main, side, classes = set(), set(), {}
    for person in people:
        for p in person['ml']['papers']:
            key = p['workId']
            (main if p['scope'] == 'main' else side).add(key)
            classes.setdefault(p['publicationClass'], set()).add(key)
    affiliations = [person.get('provenance', {}).get('primaryAffiliation', {}) for person in people]
    result.update(sitePeople=len(people), mainCrossoverPeople=sum(p['ml']['hasMain'] for p in people),
                  sideOnlyPeople=sum(p['ml']['hasSide'] and not p['ml']['hasMain'] for p in people),
                  uniqueMainPapers=len(main), uniqueSidePapers=len(side),
                  publicationClassCounts={k: len(classes.get(k, set())) for k in previous['publicationClassCounts']},
                  dbServiceEvents=sum(len(p['db']['events']) for p in people),
                  primaryAffiliationConflicts=sum(bool(e.get('hasConflict')) for e in affiliations),
                  primaryAffiliationLowConfidence=sum(e.get('confidence') == 'low' for e in affiliations),
                  primaryAffiliationReviewRows=sum(bool(e.get('hasConflict')) or e.get('confidence') != 'high'
                                                   for e in affiliations))
    return result
