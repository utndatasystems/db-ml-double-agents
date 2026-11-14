"""Record-level evidence for Sankey nodes and exact source-to-target subsets."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing

try:
    from .publication_category_reviews import load_category_reviews
except ImportError:
    from publication_category_reviews import load_category_reviews


def csv_rows(path):
    if path.exists():
        with path.open(encoding='utf-8-sig', newline='') as handle:
            yield from csv.DictReader(handle)


def joined(values):
    return '; '.join(sorted({str(v) for v in values if v is not None and str(v)}))


def matching_note(note):
    """Read the known profile-review envelope without interpreting arbitrary notes."""
    prefix = 'Service identity profile correction: '
    if not isinstance(note, str) or not note.startswith(prefix):
        return note or '', {}, []
    try:
        payload = json.loads(note[len(prefix):])
    except (TypeError, ValueError):
        return note, {}, []
    evidence = payload.get('evidence') if isinstance(payload, dict) else None
    if not isinstance(evidence, dict) or not isinstance(evidence.get('reason'), str):
        return note, {}, []
    urls = evidence.get('evidence_urls', [])
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        return note, {}, []
    provenance = {'Identity decision route': evidence.get('rule', ''),
                  'Identity reviewed on': evidence.get('reviewed_on', ''),
                  'Identity review sources': joined(urls)}
    return evidence['reason'], provenance, urls


def category(row):
    return ('matched' if row.get('resolved_dblp_pid') else 'ambiguous'
            if row.get('status', '').startswith('ambiguous') else 'candidate'
            if row.get('all_candidate_count') else 'unmatched')


def lookup(row):
    count = row.get('all_candidate_count', 0)
    return 'none' if not count else 'one' if count == 1 else 'several'


def service_record(row, identifier, decision):
    return {'id': identifier, 'label': row.get('name') or 'Unnamed service entry',
            'detail': decision, 'url': row.get('source_url') or '',
            'evidence': {'Venue': row.get('venue', ''),
                         'Service year': row.get('year', row.get('conference_year', '')),
                         'Role': row.get('role', ''), 'Affiliation': row.get('affiliation', ''),
                         'Decision': decision, 'Normalized name': row.get('name_key', ''),
                         'Original source text': row.get('raw_text', ''),
                         'Name validation': row.get('name_validation_reason', ''),
                         'Source database': row.get('source_db', ''),
                         'Source row': row.get('source_rowid', '')}}


def people_records(data_dir, flow, all_people, policy):
    with closing(sqlite3.connect(data_dir / 'entity_resolution.sqlite')) as conn:
        conn.row_factory = sqlite3.Row
        observations = [dict(r) for r in conn.execute('SELECT * FROM reviewer_observations')]
        names = {r['name_key']: dict(r) for r in conn.execute('SELECT * FROM name_resolution')}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        duplicates = {(r['source_db'], r['source_rowid']) for r in conn.execute('SELECT * FROM technical_duplicate_log')} if 'technical_duplicate_log' in tables else set()
    records, edges = defaultdict(list), {}
    observed = defaultdict(list)
    for index, row in enumerate(observations):
        record = service_record(row, f"observation:{row.get('observation_id', index)}", 'Retained for name grouping')
        records['intake'].append(record)
        if row.get('name_is_valid', 1):
            observed[row['name_key']].append((row, record))
        else:
            records['invalid-names'].append(service_record(row, record['id'], 'Failed person-name validation'))
    records['stored-observations'] = records['intake']
    traced = {(r.get('source_db'), r.get('source_rowid')) for r in observations}
    for path in sorted(data_dir.glob('*_reviewers.sqlite')):
        with closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_records'").fetchone():
                continue
            for row_ in conn.execute('SELECT rowid AS source_rowid,* FROM reviewer_records'):
                row = dict(row_); row['source_db'] = path.name
                ref = (path.name, row['source_rowid'])
                status = ('Outside seed roles' if row.get('qualifies_db_seed', 1) not in (1, None)
                          else 'Ingested' if ref in traced else 'Technical duplicates' if ref in duplicates
                          else 'Empty name' if not row.get('name', '').strip() else 'Not traced to intake')
                item = service_record(row, f"{path.name}:{row['source_rowid']}", status)
                source = 'source-' + path.stem
                records[source].append(item)
                target = 'intake' if status == 'Ingested' else 'intake-' + status
                edges.setdefault((source, target), []).append(item)
                if status != 'Ingested':
                    records[target].append(item)
    by_pid = defaultdict(list)
    for key, entries in sorted(observed.items()):
        resolution = names.get(key, {})
        pid = resolution.get('resolved_dblp_pid')
        reason, provenance, review_urls = matching_note(resolution.get('notes'))
        item = {'id': key, 'label': resolution.get('representative_name') or entries[0][0].get('name') or key,
                'detail': reason or resolution.get('status', 'No identity decision'),
                'url': 'https://dblp.org/pid/' + pid if pid else '',
                'sources': sorted({r.get('source_url') for r, _ in entries if r.get('source_url')}),
                'reviewSources': review_urls,
                'evidence': {'Service names': joined(r.get('name') for r, _ in entries),
                             'Affiliations': joined(r.get('affiliation') for r, _ in entries),
                             'Service venues': joined(r.get('venue') for r, _ in entries),
                             'Service years': joined(r.get('year') for r, _ in entries),
                             'Service entries': len(entries),
                             'DBLP candidates': resolution.get('all_candidate_count', 'Not recorded'),
                             'Identity status': resolution.get('status', 'No identity decision'),
                             'Matching reason': reason, 'Assigned DBLP PID': pid or '', **provenance}}
        records['name-groups'].append(item)
        # Deterministic accounting representatives only; every entry remains in
        # service evidence. This ordering does not decide identity or eligibility.
        for row, repeat in entries[1:]:
            repeat = {**repeat, 'detail': 'Additional service entry for the same normalized name',
                      'evidence': {**repeat['evidence'], 'Decision': 'Grouped with another service entry',
                                   'Representative entry': entries[0][1]['id']}}
            records['repeat-service'].append(repeat)
        if key not in names:
            records['no-decision'].append(item)
            continue
        node = 'lookup-' + lookup(resolution)
        records[node].append(item)
        decision = category(resolution)
        records[decision].append(item)
        edges.setdefault((node, decision), []).append(item)
        if pid:
            by_pid[pid].append(item)
    by_person = {p['dblpPid']: p for p in all_people}
    candidates = {p['dblpPid']: p for p in policy['candidates']}
    for pid, aliases in sorted(by_pid.items()):
        person = by_person.get(pid)
        focus = (candidates.get(pid) or person or {}).get('focus', {})
        events = (person or {}).get('db', {}).get('events', [])
        item = {'id': pid, 'label': person['name'] if person else aliases[0]['label'],
                'url': 'https://dblp.org/pid/' + pid,
                'detail': focus.get('reason', 'Identity matched; no retained website person'),
                'sources': sorted({e['sourceUrl'] for e in events if e.get('sourceUrl')}),
                'evidence': {'DBLP PID': pid, 'Matched service names': joined(a['label'] for a in aliases),
                             'Service venues': joined(e.get('venue') for e in events),
                             'Service roles': joined(e.get('role') for e in events),
                             'Service years': joined(e.get('year') for e in events),
                             'PC eligibility': 'Qualifying PC member' if pid in candidates else 'Outside selected PC scope',
                             'DB conference papers': focus.get('dbPapers', 'Not recorded'),
                             'ML conference papers': focus.get('mlPapers', 'Not recorded'),
                             'Publication years': focus.get('publicationYears', 'Not recorded'),
                             'DB publication years': focus.get('dbPublicationYears', 'Not recorded'),
                             'Qualifying PC service years': focus.get('serviceYears', 'Not recorded'),
                             'Focus classification': focus.get('label', 'Not recorded'),
                             'Decision route': focus.get('basis', ''), 'Focus reason': focus.get('reason', '')}}
        if person and focus.get('included'):
            item['profileId'] = person['id']
        records['identities'].append(item)
        for alias in aliases[1:]:
            records['aliases'].append({**alias, 'detail': f'Additional service name merged into DBLP {pid}',
                'evidence': {**alias['evidence'], 'Merged into': aliases[0]['label'], 'DBLP PID': pid}})
        target = 'pc' if pid in candidates else 'outside-pc' if person else 'people-not-exported'
        records[target].append(item)
        if pid in candidates:
            label = focus.get('label')
            records[{'DB-focused': 'focus-0', 'Limited evidence': 'focus-1', 'ML-focused': 'focus-2', 'Mixed': 'focus-3'}[label]].append(item)
    return records, edges


def paper_records(data_dir, flow, all_works, cohort_works, cohort):
    records, edges = defaultdict(list), {}
    category_reviews = load_category_reviews(data_dir / 'publication_category_reviews.csv')
    raw, author_links, author_names = {}, defaultdict(list), {}
    for line, row in enumerate(csv_rows(data_dir / 'dblp_publications_for_db_people.csv'), 2):
        key = row.get('dblp_key', '').strip() or f'dblp_publications_for_db_people.csv:line-{line}'
        raw.setdefault(key, {k: row.get(k, '') for k in ('title', 'year', 'record_type', 'venue_label', 'url')})
        author_names[row.get('dblp_pid', '')] = row.get('canonical_name') or row.get('dblp_pid', '')
        item = {'id': f'author-link:{line}', 'label': row.get('title') or key,
                'detail': f"{row.get('canonical_name', row.get('dblp_pid', ''))} · {row.get('year', '')}",
                'url': row.get('url') or ('https://dblp.org/rec/' + key),
                'evidence': {'DBLP record': key, 'Researcher': row.get('canonical_name', ''),
                             'Researcher PID': row.get('dblp_pid', ''),
                             'Author signature': row.get('author_signature', ''),
                             'Author match rule': row.get('match_basis', 'Not recorded'),
                             'Author position': row.get('author_position', ''),
                             'Author count': row.get('author_count', ''), 'Year': row.get('year', '')}}
        records['accepted'].append(item)
        author_links[key].append(item)
    records['candidates'] = records['accepted']
    resolution = {r['dblp_key']: r for r in csv_rows(data_dir / 'publication_entity_resolution.csv') if r['dblp_key'] in raw}
    versions, owners = defaultdict(list), defaultdict(set)
    for key, row in raw.items():
        decision = resolution.get(key, {})
        work = decision.get('work_id')
        item = {'id': key, 'label': row['title'] or key,
                'detail': f"{row['year']} · {row['venue_label']}",
                'url': row['url'] or 'https://dblp.org/rec/' + key,
                'evidence': {'DBLP record': key, 'Year': row['year'], 'Record type': row['record_type'],
                             'Tracked authors': joined(a['evidence']['Researcher'] or a['evidence']['Researcher PID'] for a in author_links[key]),
                             'Author links': len(author_links[key]), 'Work ID': work or 'No version decision',
                             'Version-linking rule': decision.get('resolution_basis', 'No version decision'),
                             'Canonical DBLP record': decision.get('canonical_dblp_key', ''),
                             'Canonical record': decision.get('is_canonical', '')}}
        records['records'].append(item)
        for link in author_links[key][1:]:
            records['coauthors'].append({**link, 'evidence': {**link['evidence'],
                'Decision': 'Additional tracked-author link; publication already counted once',
                'Representative link': author_links[key][0]['id']}})
        if work:
            versions[work].append(item)
            owners[work].update(a['evidence']['Researcher PID'] for a in author_links[key])
        else:
            records['undecided'].append(item)
    audit = defaultdict(list)
    cohort_ids = {p['dblpPid'] for p in cohort}
    for row in csv_rows(data_dir / 'reports/focus_publication_audit.csv'):
        if row.get('work_id') and row.get('pid') in cohort_ids:
            audit[row['work_id']].append(row)
    node_ids = {n['id'] for n in flow['nodes']}
    for work in sorted(set(versions) | set(all_works)):
        copies = sorted(versions.get(work, []), key=lambda r: (r['evidence'].get('Canonical record') != '1', r['id']))
        papers = all_works.get(work, [])
        paper = papers[0] if papers else {}
        first = copies[0] if copies else {'label': paper.get('title', work), 'url': paper.get('url', ''), 'detail': ''}
        item = {'id': work, 'label': paper.get('title') or first['label'], 'url': paper.get('url') or first['url'],
                'detail': f"{paper.get('year', '')} · {paper.get('venueLabel', '')}" if papers else first['detail'],
                'evidence': {'Work ID': work, 'Year': paper.get('year') or (first.get('evidence') or {}).get('Year', ''),
                             'DBLP versions': joined(c['id'] for c in copies),
                             'Version-linking rules': joined(c['evidence']['Version-linking rule'] for c in copies),
                             'Tracked authors': joined(f'{author_names.get(pid, pid)} ({pid})' for pid in owners[work]),
                             'Study authors': joined(f'{author_names.get(pid, pid)} ({pid})' for pid in owners[work] & cohort_ids),
                             'Publication category': joined(p.get('publicationClass') for p in papers),
                             'DB evidence decision': joined(r.get('reason') for r in audit[work]),
                             'DB evidence pages': joined(r.get('pages') for r in audit[work]),
                             'Decision evidence sources': joined(r.get('exclusion_evidence_url') for r in audit[work])}}
        reviewed = [(copy['id'], category_reviews[copy['id']]) for copy in copies
                    if copy['id'] in category_reviews]
        if reviewed:
            item['evidence'].update({
                'Display-category review': joined(f"{key}: {review['status']}" for key, review in reviewed),
                'Category review date': joined(review['reviewed_on'] for _, review in reviewed),
                'Category review sources': joined(review['evidence_url'] for _, review in reviewed),
                'Category review notes': joined(f"{key}: {review['reason']}" for key, review in reviewed),
                'Category review scope': 'Profile display categories only; independent of DB-focus paper eligibility.'})
        if copies:
            records['works'].append(item)
            for extra in copies[1:]:
                records['versions'].append({**extra, 'evidence': {**extra['evidence'],
                    'Decision': 'Additional version of an existing work; not an additional publication',
                    'Counting representative': first['id']}})
        else:
            records['untraced-work'].append(item)
        if work in all_works:
            records['website'].append(item)
            records['cohort' if work in cohort_works else 'outside'].append(item)
        elif 'invalid-work' in node_ids or 'missing-work' in node_ids:
            # Membership of validation failures comes from the existing audited
            # node lists, not an inference from absent website metadata.
            records['skipped'].append(item)
    # Analysis-use partitions were already decided from the full focus audit.
    return records, edges


def complete_flow_drilldowns(data_dir, flows, all_people, cohort, policy, all_works, cohort_works, groups, register, existing_metadata):
    """Attach exact lists; never substitute an entire target for a split ribbon."""
    metadata = {}
    def collect(value):
        if isinstance(value, dict):
            if value.get('id') and 'count' in value and value['id'] in groups:
                metadata[value['id']] = value
            for child in list(value.values()):
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect([flows, existing_metadata])
    for kind in ('people', 'papers'):
        flow = flows[kind]
        if flow.get('unavailable'):
            continue
        node_records, edge_records = (people_records(data_dir, flow, all_people, policy) if kind == 'people'
            else paper_records(data_dir, flow, all_works, cohort_works, cohort))
        # Node and split-ribbon lists are independently constructed. Preserve
        # the same stored candidate evidence on both without widening a subset.
        name_evidence = {}
        if kind == 'people':
            name_ids = {r['id'] for r in node_records.get('name-groups', [])}
            for records in groups.values():
                for record in records:
                    if record['id'] in name_ids:
                        name_evidence[record['id']] = {**name_evidence.get(record['id'], {}), **record}
        def enrich_name(record):
            previous = name_evidence.get(record['id'], {})
            return {**previous, **record,
                    'evidence': {**previous.get('evidence', {}), **record.get('evidence', {})}}
        work_evidence = {r['id']: r for key in ('works', 'website') for r in node_records.get(key, [])}
        decisions = {'website': 'Retained in matched researchers\' profiles.',
                     'cohort': 'At least one DB-focused author; retained for study analysis and profile context.',
                     'outside': 'Retained profile context; no DB-focused author.',
                     'main': 'Selected main-conference ML paper used in the study.',
                     'db': 'Qualifying DB conference evidence used to classify publication focus.',
                     'uncertain': 'Retained profile context; DB-paper length or snapshot evidence is unverified and supplies no DB-focus evidence.',
                     'context': 'Retained profile context; outside selected main-ML and qualifying-DB evidence.'}
        def stage_record(record, node):
            if kind == 'people' and node['stage'] in (2, 3, 4) and record['id'] in name_evidence:
                record = enrich_name(record)
            if kind == 'papers' and node['id'] in decisions:
                record = {**record, 'evidence': {**record.get('evidence', {}),
                                               'Stage decision': decisions[node['id']]}}
            return record
        for node in flow['nodes']:
            old_id = node.get('groupId')
            old = {r['id']: r for r in groups.get(old_id, [])}
            items = node_records.get(node['id'])
            if items is None and old_id:
                items = [{**r, 'evidence': {**r.get('evidence', {}),
                    **work_evidence.get(r['id'], {}).get('evidence', {})}} for r in old.values()]
            if items is None:
                items = []
            items = [stage_record({**old.get(r['id'], {}), **r}, node) for r in items]
            key = old_id or f'flow-{kind}-node-{node["id"]}'
            if len(items) != node['count']:
                if node['id'] in ('ambiguous-authors', 'candidates'):
                    # Historical scan logs may save only rejected-link counts.
                    node['recordsUnavailable'] = 'Only the aggregate author-rejection count was saved; individual rejected links are unavailable.'
                    items = []
                else:
                    raise ValueError(f"Sankey record list does not reconcile: {kind}/{node['id']}: {len(items)} != {node['count']}")
            description = node.get('recordsUnavailable') or node['description']
            detail = register(key, metadata.get(key, {}).get('label', node['label']), items, node['color'], description)
            detail.update(unit=node['unit'], scope=('Collected service records' if kind == 'people' and node['stage'] <= 2
                else 'Collected service names' if kind == 'people' and node['stage'] <= 4
                else 'Qualifying PC members' if kind == 'people' and node['stage'] == 7
                else 'Study cohort' if kind == 'papers' and node['stage'] >= 5 and node['id'] != 'outside'
                else 'All matched researchers'), stage=flow['stages'][node['stage']])
            if node.get('recordsUnavailable'):
                detail.update(recordsUnavailable=node['recordsUnavailable'], representedCount=node['count'])
            node['groupId'] = key
            metadata[key] = detail
        nodes = {n['id']: n for n in flow['nodes']}
        for link in flow['links']:
            source, target = nodes[link['source']], nodes[link['target']]
            items = edge_records.get((source['id'], target['id']))
            if items is None and link['value'] == target['count']:
                items = groups[target['groupId']]
            if items is None:
                # The only multi-input publication node partitions by whether
                # its work IDs have a source version decision.
                source_ids = {r['id'] for r in groups[source['groupId']]}
                items = [r for r in groups[target['groupId']] if r['id'] in source_ids]
            items = [stage_record(record, target) for record in items]
            if len(items) != link['value'] and not (source.get('recordsUnavailable') or target.get('recordsUnavailable')):
                raise ValueError(f"Sankey ribbon records do not reconcile: {kind}/{source['id']} -> {target['id']}")
            key = re.sub(r'[^A-Za-z0-9_-]', '-', f'flow-{kind}-edge-{source["id"]}--{target["id"]}')
            detail = register(key, source['label'] + ' → ' + target['label'], items, target['color'],
                              f"Records flowing from {source['label']} to {target['label']}. " + target['description'])
            detail.update(unit=target['unit'], scope=metadata.get(target['groupId'], {}).get('scope', ''),
                          stage=flow['stages'][source['stage']] + ' → ' + flow['stages'][target['stage']])
            if source.get('recordsUnavailable') or target.get('recordsUnavailable'):
                detail.update(recordsUnavailable=source.get('recordsUnavailable') or target['recordsUnavailable'], representedCount=link['value'])
            link['groupId'] = key
