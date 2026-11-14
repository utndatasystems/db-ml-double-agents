"""Export pipeline coverage and lazy audit lists without changing study eligibility."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

try:
    from .focus_population import publication_profile, qualifying_pc_events
    from .coverage_flow import people_flow, papers_flow, limited_evidence_reason
    from .coverage_flow_records import complete_flow_drilldowns
    from .coverage_candidates import CandidateReview
    from .identity_review import sparse_exact_review_groups
    from .ror_affiliations import IdentityRorAffiliationIndex
    from .publication_category_reviews import load_category_reviews
except ImportError:
    from focus_population import publication_profile, qualifying_pc_events
    from coverage_flow import people_flow, papers_flow, limited_evidence_reason
    from coverage_flow_records import complete_flow_drilldowns
    from coverage_candidates import CandidateReview
    from identity_review import sparse_exact_review_groups
    from ror_affiliations import IdentityRorAffiliationIndex
    from publication_category_reviews import load_category_reviews

LABELS = {'main_ml': 'Main ML conference', 'database': 'Database', 'other_conference': 'Other conference',
          'journal': 'Journal', 'workshop': 'Workshop', 'preprint': 'Preprint', 'other': 'Other records',
          'conflict': 'Conflicting categories'}
COLORS = {'main_ml': 'accent', 'database': 'database', 'other_conference': 'conference', 'journal': 'journal',
          'workshop': 'workshop', 'preprint': 'muted', 'other': 'quiet', 'conflict': 'conflict'}


def publication_row_issues(row):
    """Required fields checked by the website loader, before it skips a row."""
    issues = []
    try:
        int(row.get('year') or '')
    except ValueError:
        issues.append('missing or invalid year')
    for field, label in [('title', 'missing title'), ('dblp_key', 'missing DBLP key')]:
        if not (row.get(field) or '').strip():
            issues.append(label)
    return issues


def audit_publication_input(path, people):
    """Inspect raw input, including rows that never reach the retained bibliography."""
    affected, records = set(), {}
    if not path.exists():
        return None, affected
    by_pid = {p['dblpPid']: p for p in people}
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for line, row in enumerate(csv.DictReader(handle), 2):
            pid = (row.get('dblp_pid') or '').strip()
            issues = publication_row_issues(row)
            if pid not in by_pid or not issues:
                continue
            affected.add(pid)
            key = (row.get('dblp_key') or '').strip()
            identifier = key or f'{path.name}:line-{line}'
            record = records.setdefault(identifier, {'id': identifier,
                'label': (row.get('title') or '').strip() or 'Untitled record',
                'url': 'https://dblp.org/rec/' + key if key else '', 'issues': set(), 'people': set()})
            record['issues'].update(issues)
            record['people'].add(by_pid[pid]['name'])
    for record in records.values():
        record['detail'] = '; '.join(sorted(record.pop('issues'))) + '. Researchers: ' + ', '.join(sorted(record.pop('people')))
    return list(records.values()), affected


def length_review_impact(audit_rows, people, candidates):
    """Scenario: every unverified DB work proves to be a full paper.

    Reuse the cohort policy and work/year deduplication. This estimates possible
    additions; it does not accept records or change anyone's classification.
    """
    by_pid = {p['dblpPid']: p for p in people}
    uncertain, accepted = defaultdict(set), defaultdict(set)
    affected, unknown = set(), {}
    for row in audit_rows:
        pid = row['pid']
        if pid not in by_pid:
            continue
        if row['counted'] == 'True':
            accepted[pid].add(row['key'])
        if row['reason'] == 'Full-paper length cannot be verified':
            uncertain[pid].add(row['key'])
            affected.add(pid)
            record = unknown.setdefault(row['key'], {'id': row['key'], 'label': row['title'],
                'url': 'https://dblp.org/rec/' + row['key'], 'people': set()})
            record['people'].add(by_pid[pid]['name'])
    potential = []
    for pid in sorted(affected & set(candidates)):
        person = by_pid[pid]
        if person['focus']['included']:
            continue
        db_years, ml_years = {}, {}
        for paper in person['ml']['papers']:
            work = paper['workId']
            if paper['scope'] == 'main':
                ml_years[work] = paper['year']
                continue
            years = [v['year'] for v in [paper, *paper.get('resolvedVersions', [])]
                     if v['dblpKey'] in accepted[pid] | uncertain[pid] and isinstance(v.get('year'), int)]
            if years:
                db_years[work] = min(years)
        profile = publication_profile(len(db_years), len(ml_years), set(db_years.values()) | set(ml_years.values()),
            db_years=db_years.values(), service_years={e['year'] for e in qualifying_pc_events(person)})
        if profile['label'] == 'DB-focused':
            potential.append(person_record(person, f"Could qualify if all unverified DB works are full papers: {len(db_years)} DB / {len(ml_years)} ML papers. Current classification: {person['focus']['label']}."))
    for record in unknown.values():
        record['detail'] = 'Full-paper length unverified. Affected researchers: ' + ', '.join(sorted(record.pop('people')))
    return list(unknown.values()), affected, potential


def assignment_route(paper):
    if paper['scope'] == 'main':
        return 'main'
    if paper['publicationClass'] == 'preprint':
        return 'preprint'
    if paper.get('categoryMapping'):
        return 'mapping'
    if paper['publicationClass'] == 'workshop':
        return 'workshop'
    if paper['publicationClass'] in {'journal', 'other_conference'}:
        return 'structural'
    return 'other'


def person_record(person, detail):
    return {'id': person['dblpPid'], 'label': person['name'], 'detail': detail,
            'url': 'https://dblp.org/pid/' + person['dblpPid'],
            'profileId': person['id'] if person.get('focus', {}).get('included') else '',
            'sources': sorted({e['sourceUrl'] for e in person['db']['events'] if e.get('sourceUrl')})}


def work_record(key, papers):
    paper = papers[0]
    return {'id': key, 'label': paper['title'],
            'detail': f"{paper.get('year', '')} · {paper.get('venueLabel', '')}",
            'url': paper.get('url') or 'https://dblp.org/rec/' + paper['dblpKey']}


def work_inventory(people):
    works = defaultdict(list)
    for person in people:
        for paper in person['ml']['papers']:
            works[paper['workId']].append(paper)
    return dict(works)


def pending_category_records(data_dir, works):
    """Display-category questions are work-level flags, not missing DB evidence."""
    reviews = {key: review for key, review in load_category_reviews(
        data_dir / 'publication_category_reviews.csv').items() if review['status'] == 'pending'}
    records = []
    for key, papers in works.items():
        version_keys = {version.get('dblpKey') for paper in papers
                        for version in [paper, *paper.get('resolvedVersions', [])]}
        pending = [reviews[version] for version in sorted(version_keys - {None}) if version in reviews]
        if not pending:
            continue
        record = work_record(key, papers)
        record['detail'] = 'Retained publication; its display category needs track evidence. This is separate from DB-focus eligibility.'
        record['reviewSources'] = sorted({review['evidence_url'] for review in pending})
        record['reviewSourceLabel'] = 'Publication category sources'
        record['evidence'] = {
            'Work ID': key,
            'Review status': 'Pending display-category review; no counting change',
            'Reviewed DBLP records': '; '.join(review['dblp_key'] for review in pending),
            'Current display category': '; '.join(sorted({p['publicationClass'] for p in papers})),
            'Reviewed on': '; '.join(sorted({review['reviewed_on'] for review in pending})),
            'Category evidence sources': '; '.join(record['reviewSources']),
            'Open track questions': '; '.join(review['reason'] for review in pending),
        }
        records.append(record)
    return records


def work_categories(works):
    groups = {key: [] for key in LABELS}
    for key, papers in works.items():
        classes = {p['publicationClass'] for p in papers}
        category = next(iter(classes)) if len(classes) == 1 else 'conflict'
        category = category if category in groups else 'other'
        record = work_record(key, papers)
        if category == 'conflict':
            record['detail'] += ' · Labels: ' + ', '.join(sorted(classes))
        groups[category].append(record)
    return groups


def export_coverage(data_dir: Path, output_dir: Path, all_people, cohort, policy):
    """Every stack is an exclusive partition of one explicitly named unit."""
    groups = {}
    def group(key, label, records, color, description):
        records = sorted(records, key=lambda r: (r['label'].casefold(), r['id']))
        groups[key] = records
        return {'id': key, 'label': label, 'count': len(records), 'color': color, 'description': description}

    conn = sqlite3.connect(data_dir / 'entity_resolution.sqlite')
    conn.row_factory = sqlite3.Row
    ror_path = data_dir / 'ror_snapshot' / 'ror_affiliation_index.sqlite'
    ror = IdentityRorAffiliationIndex(ror_path) if ror_path.exists() else None
    try:
        name_rows = conn.execute('SELECT r.*, g.affiliations_json, g.venues_json FROM name_resolution r JOIN name_groups g USING(name_key)').fetchall()
        source_urls = defaultdict(set)
        for row in conn.execute('SELECT name_key, source_url FROM reviewer_observations WHERE source_url IS NOT NULL'):
            if row['source_url']:
                source_urls[row['name_key']].add(row['source_url'])
        matching = defaultdict(list)
        candidate_review = CandidateReview(conn, ror)
        for row in name_rows:
            if row['resolved_dblp_pid']:
                status = 'matched'
            elif row['status'].startswith('ambiguous'):
                status = 'ambiguous'
            elif row['all_candidate_count']:
                status = 'candidate'
            else:
                status = 'unmatched'
            detail = f"{row['status'].replace('_', ' ')} · {row['all_candidate_count']} DBLP candidates"
            record = {'id': row['name_key'], 'label': row['representative_name'], 'detail': detail,
                                     'url': 'https://dblp.org/pid/' + row['resolved_dblp_pid'] if row['resolved_dblp_pid'] else '',
                                     'sources': sorted(source_urls[row['name_key']])}
            if status in ('ambiguous', 'candidate'):
                record['identityReview'] = candidate_review.for_name(row)
            matching[status].append(record)
        matched_by_name = {r['id']: r for r in matching['matched']}
        rows_by_name = {r['name_key']: r for r in name_rows}
        sparse_identity_review = []
        for item in sparse_exact_review_groups(conn):
            row = rows_by_name.get(item['name_key'])
            if row is None or row['name_key'] not in matched_by_name:
                continue
            record = dict(matched_by_name[row['name_key']])
            record['identityReview'] = candidate_review.for_name(row)
            sparse_identity_review.append(record)
        identity_review = []
        for person in all_people:
            alternatives = {}
            for resolution in conn.execute('SELECT r.*,g.affiliations_json FROM name_resolution r JOIN name_groups g USING(name_key) WHERE resolved_dblp_pid=?', (person['dblpPid'],)):
                score = conn.execute('SELECT MAX(affiliation_similarity) FROM dblp_candidates WHERE name_key=? AND dblp_pid=?',
                                     (resolution['name_key'], person['dblpPid'])).fetchone()[0] or 0
                for alternative in conn.execute('SELECT c.*, p.canonical_name FROM dblp_candidates c JOIN dblp_people p USING(dblp_pid) '
                                                'WHERE c.name_key=? AND c.dblp_pid<>? AND c.affiliation_similarity>? AND c.affiliation_similarity>0',
                                                (resolution['name_key'], person['dblpPid'], score)):
                    if candidate_review.credible_alternative(resolution, alternative):
                        alternatives[alternative['dblp_pid']] = alternative['canonical_name']
            if alternatives:
                record = person_record(person, 'Another name candidate has stronger recorded affiliation evidence. Review required; this is not a confirmed error.')
                record['alternatives'] = [{'label': name, 'url': 'https://dblp.org/pid/' + pid} for pid, name in alternatives.items()]
                identity_review.append(record)
        observations = conn.execute('SELECT COUNT(*) FROM reviewer_observations').fetchone()[0]
    finally:
        conn.close()
        if ror:
            ror.close()
    people_stacks = [{
        'id': 'name-matching', 'title': 'Can we match the collected service names?', 'unit': 'name groups', 'total': len(name_rows),
        'note': 'Matched names are merged by DBLP identity before study selection. A match is a pipeline decision, not a guarantee of correctness.',
        'groups': [group('names-'+key, label, matching[key], color, description) for key, label, color, description in [
            ('matched', 'Matched to DBLP', 'accent', 'Names with an assigned DBLP identity.'),
            ('ambiguous', 'Ambiguous match', 'attention', 'The available evidence does not identify one person confidently.'),
            ('candidate', 'Candidate needs verification', 'sage', 'A possible name variant exists but has not met the matching rules.'),
            ('unmatched', 'No candidate found', 'muted', 'No local DBLP candidate was found for the recorded name.')]],
    }]
    candidate_profiles = {p['dblpPid']: p for p in policy['candidates']}
    eligibility = defaultdict(list)
    for person in all_people:
        candidate = candidate_profiles.get(person['dblpPid'])
        label = candidate['focus']['label'] if candidate else 'Outside PC scope'
        focus = person['focus']
        detail = f"{focus['dbPapers']} DB / {focus['mlPapers']} ML conference papers. " + (focus['reason'] if candidate else 'No qualifying PC role in the selected venue/year coverage.')
        eligibility[label].append(person_record(person, detail))
    people_stacks.append({
        'id': 'study-selection', 'title': 'Which matched researchers enter the study?', 'unit': 'DBLP identities', 'total': len(all_people),
        'note': 'Focus and PC-role exclusions are study rules. Limited evidence may reflect sparse records, incomplete matching or a different publication history.',
        'groups': [group('people-'+str(i), label, eligibility[label], color, description) for i, (label, color, description) in enumerate([
            ('DB-focused', 'accent', 'Included through the publication rule or repeated-PC-service fallback.'),
            ('Limited evidence', 'sage', 'Neither evidence route is met by the current records.'),
            ('ML-focused', 'green', 'The publication balance is ML-focused under the selected-venue rule.'),
            ('Mixed', 'dark-green', 'Neither field outnumbers the other two to one.'),
            ('Outside PC scope', 'muted', 'Service exists, but no qualifying PC role falls within the selected coverage.')])],
    })
    all_works, cohort_works = work_inventory(all_people), work_inventory(cohort)
    paper_stacks = []
    raw_path = data_dir / 'dblp_publications_for_db_people.csv'
    resolution_path = data_dir / 'publication_entity_resolution.csv'
    collection = {'available': raw_path.exists() and resolution_path.exists(), 'serviceObservations': observations}
    if collection['available']:
        raw = {}
        links = 0
        with raw_path.open(encoding='utf-8-sig', newline='') as handle:
            for line, row in enumerate(csv.DictReader(handle), 2):
                links += 1
                key = row['dblp_key'].strip() or f'{raw_path.name}:line-{line}'
                raw.setdefault(key, {'id': key, 'label': row['title'] or 'Untitled record', 'detail': row['year'],
                                    'url': row['url'] or ('https://dblp.org/rec/' + row['dblp_key'] if row['dblp_key'] else '')})
        records = defaultdict(list)
        assigned = set()
        with resolution_path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                if row['dblp_key'] not in raw or row['dblp_key'] in assigned:
                    continue
                assigned.add(row['dblp_key'])
                record = dict(raw[row['dblp_key']])
                record['detail'] += ' · ' + row['resolution_basis'].replace('_', ' ')
                records['canonical' if row['is_canonical'] == '1' else 'linked'].append(record)
        records['unresolved'] = [record for key, record in raw.items() if key not in assigned]
        assert sum(map(len, records.values())) == len(raw), 'Publication record coverage does not reconcile'
        collection.update(records=len(raw), personPaperLinks=links, distinctWorks=len(all_works))
        paper_stacks.append({'id': 'version-linking', 'title': 'How are publication versions handled?', 'unit': 'DBLP records',
            'total': len(raw), 'note': 'Linked versions are retained as evidence but do not count as extra works. Coauthorship links are not extra publications.',
            'groups': [group('papers-canonical', 'Work representatives', records['canonical'], 'accent', 'The canonical records representing distinct works.'),
                       group('papers-linked', 'Linked versions', records['linked'], 'sage', 'Preprints or other versions linked to a canonical work.'),
                       group('papers-unresolved', 'No version decision', records['unresolved'], 'attention', 'Source records absent from the version-resolution export. They may be incomplete or require investigation.') ]})
    included_keys = set(cohort_works)
    paper_stacks.append({'id': 'publication-use', 'title': 'Which works reach the publication charts?', 'unit': 'distinct works',
        'total': len(all_works), 'note': 'Context publications stay available in researcher profiles. Works outside the cohort remain in the pipeline; they are not missing records.',
        'groups': [
            group('works-main', 'Main ML: plotted', [work_record(k, ps) for k, ps in cohort_works.items() if any(p['scope'] == 'main' for p in ps)], 'accent', 'Main NeurIPS, ICML and ICLR works involving the included cohort.'),
            group('works-context', 'Cohort: context only', [work_record(k, ps) for k, ps in cohort_works.items() if not any(p['scope'] == 'main' for p in ps)], 'sage', 'Other works visible in profiles; they do not enter main ML publication charts.'),
            group('works-outside', 'Outside study cohort', [work_record(k, ps) for k, ps in all_works.items() if k not in included_keys], 'muted', 'Works with no author in the included study cohort.') ]})
    classifications, assignment_evidence = {}, {}
    for scope, inventory in [('cohort', cohort_works), ('all', all_works)]:
        classified = work_categories(inventory)
        classifications[scope] = {'total': len(inventory), 'groups': [group('classes-'+scope+'-'+key, LABELS[key], classified[key], COLORS[key],
            'Different researcher records assign different categories to this work.' if key == 'conflict' else 'Display category: ' + LABELS[key] + '.') for key in LABELS]}
        routes = defaultdict(list)
        for key, papers in inventory.items():
            evidence = {assignment_route(p) for p in papers}
            route = next(iter(evidence)) if len(evidence) == 1 else 'multiple'
            routes[route].append(work_record(key, papers))
        assignment_evidence[scope] = {'total': len(inventory), 'groups': [
            group('evidence-'+scope+'-'+key, label, routes[key], color, description)
            for key, label, color, description in [
                ('main', 'Exact main ML match', 'accent', 'Exact main-conference membership from the publication scan.'),
                ('mapping', 'Explicit venue mapping', 'database', 'A fixed venue-to-category rule assigns the display category.'),
                ('workshop', 'Workshop wording', 'workshop', 'Workshop wording in the venue metadata assigns the category.'),
                ('preprint', 'CoRR metadata', 'sage', 'DBLP CoRR metadata identifies a preprint.'),
                ('structural', 'DBLP type fallback', 'muted', 'The DBLP record type or journal field supplies a broad category.'),
                ('other', 'No specific assignment', 'quiet', 'No more specific display-category rule applies.'),
                ('multiple', 'Multiple assignment routes', 'conflict', 'Researcher records for this work use different assignment routes.')]]}
    # Diagnostic lists deliberately remain separate: the same record may have several flags.
    audit_rows = []
    audit_path = data_dir / 'reports/focus_publication_audit.csv'
    if audit_path.exists():
        with audit_path.open(encoding='utf-8-sig', newline='') as handle:
            audit_rows = list(csv.DictReader(handle))
    unknown_length, length_people, potential = length_review_impact(audit_rows, all_people, candidate_profiles)
    cohort_ids = {p['dblpPid'] for p in cohort}
    def impact(pids, scope='All matched researchers'):
        return {'affectedPeople': len(pids), 'includedPeople': len(pids & cohort_ids),
                'excludedPeople': len(pids - cohort_ids),
                'scope': scope}
    raw_issues, raw_people = audit_publication_input(raw_path, all_people)
    input_issues, input_people = audit_publication_input(data_dir / 'dblp_publication_entities_for_db_people.csv', all_people)
    missing_affiliations = [person_record(p, 'No usable display affiliation is recorded.') for p in cohort if not p.get('primaryAffiliation', '').strip()]
    missing_order, order_people = [], set()
    for person in cohort:
        for paper in person['ml']['papers']:
            if paper['scope'] != 'main':
                continue
            position, count = paper.get('authorPosition'), paper.get('authorCount')
            if not (isinstance(position, int) and isinstance(count, int) and count >= 1 and 0 <= position < count):
                record = work_record(paper['workId'], [paper]); record['id'] += '|' + person['dblpPid']
                record['detail'] = person['name'] + ' · Incomplete or inconsistent author order; excluded from author-position charts.'
                missing_order.append(record)
                order_people.add(person['dblpPid'])
    pending_categories = pending_category_records(data_dir, all_works)
    pending_work_ids = {record['id'] for record in pending_categories}
    pending_people = {person['dblpPid'] for person in all_people
                      if any(paper['workId'] in pending_work_ids for paper in person['ml']['papers'])}
    quality = [
        dict(group('review-identities', 'Check identity matches', identity_review, 'attention', 'An alternative name candidate has stronger affiliation evidence. This check does not find every incorrect match.'),
             unit='matched identities', **impact({r['id'] for r in identity_review})),
        dict(group('review-sparse-identities', 'Review sparse exact matches', sparse_identity_review, 'attention',
                   'Accepted exact-name matches with no recorded affiliation or ORCID and other name candidates. These need evidence review; they are not confirmed errors.'),
             unit='name groups', reviewOnly=True,
             **impact({r['identityReview']['currentPid'] for r in sparse_identity_review})),
        dict(group('review-length', 'Verify DB-paper lengths', unknown_length, 'attention', 'Article-number pagination may hide full papers. These records currently supply no DB-focus evidence.'),
             unit='DBLP records', measured=audit_path.exists(), potentialAdditions=len(potential), **impact(length_people)),
        dict(group('review-potential', 'All PC members: possible additions (what-if)', potential, 'sage', 'Broader scenario across all qualifying PC members: every unverified DB work proves to be a full paper. These excluded people are not authors in the study-cohort branch and are not added automatically.'),
             unit='people', scenario=True, measured=audit_path.exists(), **impact({r['id'] for r in potential}, scope='Qualifying PC members')),
        dict(group('review-display-categories', 'Check publication display categories', pending_categories, 'attention',
                   'Retained works with open conference-versus-workshop track questions in the manual review ledger. This is separate from unverified DB-focus evidence and does not remove publications.'),
             unit='distinct works', reviewOnly=True, **impact(pending_people)),
        dict(group('review-input', 'Records skipped before display', input_issues or [], 'attention', 'Missing or invalid year, title or DBLP key in the deduplicated input. Checked before the website loader skips records.'),
             unit='input records', measured=input_issues is not None, **impact(input_people)),
        dict(group('review-source', 'Incomplete source records', raw_issues or [], 'attention', 'Missing or invalid year, title or DBLP key in the original publication scan. May overlap with skipped input records.'),
             unit='source records', measured=raw_issues is not None, **impact(raw_people)),
        dict(group('review-affiliation', 'Missing display affiliation', missing_affiliations, 'muted', 'Checks included researchers only. A present affiliation is not proof that it is current or correct.'),
             unit='cohort researchers', affectedPeople=len(missing_affiliations), scope='Study cohort'),
        dict(group('review-order', 'Incomplete author order', missing_order, 'muted', 'Checks retained main ML papers in the study cohort. These links cannot enter author-position charts.'),
             unit='researcher–paper links', affectedPeople=len(order_people), scope='Study cohort'),
    ]
    # Keep actionable flags ahead of empty/unmeasured checks; rank by people,
    # never by comparing counts of unlike units (records versus identities).
    quality.sort(key=lambda g: (not bool(g['count']), -g['affectedPeople']))
    for stack in people_stacks + paper_stacks + list(assignment_evidence.values()):
        assert sum(g['count'] for g in stack['groups']) == stack['total'], stack.get('id', 'assignment evidence')
    flow_groups = []
    names_by_id = {r['id']: r for records in matching.values() for r in records}
    def flow_group(*args):
        if args[0].startswith('flow-lookup-'):
            args = (*args[:2], [{**names_by_id.get(r['id'], {}), **r} for r in args[2]], *args[3:])
        value = group(*args)
        flow_groups.append(value)
        return value
    flows = {'people': people_flow(data_dir.resolve(), all_people, cohort, policy, flow_group),
             'papers': papers_flow(data_dir.resolve(), all_works, cohort_works, cohort, flow_group, publication_row_issues),
             'groups': flow_groups}
    people_nodes = {n['id']: n for n in flows['people']['nodes']}
    limited = people_nodes.get('focus-1')
    if limited:
        limited['details'], limited['relatedGroupIds'] = [], []
        for key, label in [('no-papers', 'No qualifying papers'), ('ratio', 'DB:ML ratio below 2:1'),
                           ('db-evidence', 'Too little DB publication evidence'), ('pc-service', 'Only one PC service year')]:
            records = [person_record(p, p['focus']['reason']) for p in candidate_profiles.values()
                       if limited_evidence_reason(p['focus']) == key]
            if records:
                detail = flow_group('flow-limited-'+key, label, records, 'attention', label + '. These PC members remain outside the study.')
                limited['details'].append(f'{len(records):,}: {label}')
                limited['relatedGroupIds'].append(detail['id'])
        if potential:
            limited['details'].append(f'{len(potential):,} PC members could qualify if unverified DB papers prove to be full papers.')
            limited['relatedGroupIds'] += ['review-potential', 'review-length']
    matched = people_nodes.get('matched')
    if matched and (identity_review or sparse_identity_review):
        matched['details'], matched['relatedGroupIds'] = [], []
        if identity_review:
            matched['details'].append(f'{len(identity_review):,} identity assignments have alternative affiliation evidence to review.')
            matched['relatedGroupIds'].append('review-identities')
        if sparse_identity_review:
            matched['details'].append(f'{len(sparse_identity_review):,} accepted name groups have sparse identity evidence and other candidates. These are review flags, not confirmed errors.')
            matched['relatedGroupIds'].append('review-sparse-identities')
    paper_nodes = {n['id']: n for n in flows['papers'].get('nodes', [])}
    context = paper_nodes.get('context')
    if context:
        context_keys = {r['id'] for r in groups['flow-works-context']}
        categories = work_categories({k: cohort_works[k] for k in context_keys})
        context['relatedGroupIds'], counts = [], []
        for key, records in categories.items():
            if records:
                detail = flow_group('flow-context-'+key, LABELS[key], records, 'sage', 'Retained profile context: '+LABELS[key]+'.')
                context['relatedGroupIds'].append(detail['id'])
                counts.append((len(records), LABELS[key]))
        context['details'] = [f'{count:,}: {label}' for count, label in sorted(counts, reverse=True)[:3]]
    uncertain = paper_nodes.get('uncertain')
    if uncertain:
        # The flow counts cohort works; the older length audit counts records
        # across all matched people. Keep its broader diagnostics separate.
        uncertain_keys = {r['id'] for r in groups[uncertain['groupId']]}
        evidence = defaultdict(list)
        for row in audit_rows:
            if (row.get('pid') in cohort_ids and row.get('work_id') in uncertain_keys
                    and row.get('counted', '').casefold() != 'true'
                    and row.get('reason') in {'Full-paper length cannot be verified', 'Missing snapshot metadata'}):
                evidence[row['work_id']].append(row)
        records = []
        for key in sorted(uncertain_keys):
            record = work_record(key, cohort_works[key])
            record['detail'] = 'Retained in profiles; excluded from DB-focus evidence until verified.'
            record['evidence'] = {
                'Work ID': key,
                'DB evidence decision': '; '.join(sorted({row.get('reason', '') for row in evidence[key]})),
                'DBLP evidence records': '; '.join(sorted({row.get('key', '') for row in evidence[key]})),
                'Recorded pages': '; '.join(sorted({row.get('pages', '') for row in evidence[key]})),
                'Record-level DB evidence': '; '.join(sorted({
                    f"{row.get('key', '')} | pages: {row.get('pages') or 'not recorded'} | {row.get('reason', '')}"
                    for row in evidence[key]})),
                'Decision evidence sources': '; '.join(sorted({row.get('exclusion_evidence_url', '') for row in evidence[key]})),
            }
            records.append(record)
        detail = flow_group('flow-unverified-db-cohort', 'Study cohort: unverified DB works', records, 'attention',
                            'Exact work-level list for this node. These publications remain in profiles; length or snapshot evidence is insufficient for DB-focus classification.')
        detail.update(unit='distinct works', scope='Study cohort')
        uncertain['relatedGroupIds'] = [detail['id']]
        uncertain['details'] = [f"{len(records):,} retained study-cohort works need DB evidence verification."]
        if potential:
            uncertain['relatedGroupIds'].append('review-potential')
            uncertain['details'].append(f'{len(potential):,} other PC members could qualify in the broader all-unverified-papers scenario; they are outside this branch.')
    for node_id, included in [('context', True), ('outside', False)]:
        node = paper_nodes.get(node_id)
        if not node:
            continue
        node_keys = {record['id'] for record in groups[node['groupId']]}
        records = [record for record in pending_categories if record['id'] in node_keys]
        if records:
            detail = flow_group('flow-pending-categories-'+node_id,
                ('Study cohort' if included else 'Outside study cohort') + ': pending display categories',
                records, 'attention', 'Open manual-review questions about publication categories. Publications are retained; these are not unverified DB-focus evidence.')
            detail.update(unit='distinct works', scope='Study cohort' if included else 'Outside study cohort')
            node.setdefault('relatedGroupIds', []).append(detail['id'])
            node.setdefault('details', []).append(f'{len(records):,} retained works have an open display-category review; this does not affect DB-focus eligibility.')
    if missing_order and 'main' in paper_nodes:
        paper_nodes['main']['details'] = [f'{len(missing_order):,} researcher-paper links lack author order and cannot enter author-position charts.']
        paper_nodes['main']['relatedGroupIds'] = ['review-order']
    for flow in (flows['people'], flows['papers']):
        for node in flow.get('nodes', []):
            detail = next((g for g in flow_groups if g['id'] == node.get('groupId')), None)
            if detail:
                detail['unit'] = node['unit']
                if flow['id'] == 'people':
                    detail['scope'] = ('Collected service records' if node['stage'] <= 2
                                       else 'Collected service names' if node['stage'] <= 4
                                       else 'Qualifying PC members' if node['stage'] == 7
                                       else 'All matched researchers')
                else:
                    detail['scope'] = 'Study cohort' if node['stage'] == 6 else 'All matched researchers'
    for chart in people_stacks:
        for detail in chart['groups']:
            detail['unit'] = chart['unit']
            detail['scope'] = ('Collected service names' if chart['id'] == 'name-matching'
                               else 'Qualifying PC members' if detail['id'] != 'people-4'
                               else 'All matched researchers')
    for detail in flow_groups:
        if detail['id'].startswith('flow-limited-'):
            detail.update(unit='people', scope='Qualifying PC members')
        elif detail['id'].startswith('flow-context-'):
            detail.update(unit='works', scope='Study cohort')
    complete_flow_drilldowns(data_dir, flows, all_people, cohort, policy, all_works, cohort_works,
                            groups, flow_group, [people_stacks, paper_stacks, quality])
    summary = {'schemaVersion': 6, 'people': people_stacks, 'papers': paper_stacks, 'flows': flows,
               'classifications': classifications, 'assignmentEvidence': assignment_evidence, 'quality': quality, 'collection': collection,
               'limits': 'Coverage is not accuracy. These checks use collected service names and scanned publications; they cannot count people or papers the sources missed. Review flags can overlap. Study exclusions and linked versions are not data errors.'}
    detail_dir = output_dir / 'coverage'
    detail_dir.mkdir(parents=True, exist_ok=True)
    urls, content_urls = {}, {}
    for key, records in groups.items():
        text = json.dumps({'records': records}, ensure_ascii=False, separators=(',', ':')) + '\n'
        encoded = text.encode('utf-8')
        digest = hashlib.sha256(encoded).hexdigest()
        if digest in content_urls:
            urls[key] = content_urls[digest]
            continue
        name = key + '.' + digest[:12] + '.json'
        destination = detail_dir / name
        # Content-addressed lists are immutable. Reuse valid files, and publish
        # replacements atomically so a failed build cannot truncate a live list.
        if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
            temporary = destination.with_suffix('.json.tmp')
            temporary.write_bytes(encoded)
            temporary.replace(destination)
        urls[key] = 'data/coverage/' + name
        content_urls[digest] = urls[key]
    def attach(value):
        if isinstance(value, dict):
            if value.get('id') in urls:
                value['detailUrl'] = urls[value['id']]
            for child in list(value.values()):
                attach(child)
        elif isinstance(value, list):
            for child in value:
                attach(child)
    attach(summary)
    target = output_dir / 'coverage_summary.json'
    text = json.dumps(summary, ensure_ascii=False, indent=2) + '\n'
    previous_text = target.read_text(encoding='utf-8') if target.exists() else None
    # An identical rebuild repairs damaged current lists above, but must not
    # advance the retained generation or remove lists held by an open tab.
    if previous_text == text:
        return summary
    previous_names = set()
    can_prune = True
    def retain_previous(value):
        if isinstance(value, dict):
            url = value.get('detailUrl')
            if isinstance(url, str) and Path(url).parent == Path('data/coverage'):
                previous_names.add(Path(url).name)
            for child in value.values():
                retain_previous(child)
        elif isinstance(value, list):
            for child in value:
                retain_previous(child)
    if previous_text:
        try:
            previous_summary = json.loads(previous_text)
            if isinstance(previous_summary, dict):
                retain_previous(previous_summary)
            else:
                can_prune = False
        except json.JSONDecodeError:
            # Readers may still hold the preceding valid summary. Without its
            # references, retiring any old list would be unsafe.
            can_prune = False
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(target)
    # Keep one preceding changed generation for readers holding its summary.
    # Prune only older generated lists, after the new summary is published.
    current_names = {Path(url).name for url in urls.values()}
    if can_prune and current_names != previous_names:
        keep = current_names | previous_names
        for path in detail_dir.glob('*.json'):
            if path.name not in keep:
                path.unlink()
    return summary
