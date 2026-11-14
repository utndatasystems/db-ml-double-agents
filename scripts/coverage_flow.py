"""Reconciled pipeline flows. Merges are reductions in counting units, not losses."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing


class Flow:
    def __init__(self, key, stages):
        self.data = {'id': key, 'stages': stages, 'nodes': [], 'links': [], 'notes': []}

    def node(self, key, label, count, stage, unit, description, color='sage', group=None):
        if count < 0:
            raise ValueError(f'Negative pipeline count: {key}')
        self.data['nodes'].append(dict(id=key, label=label, count=count, stage=stage,
            unit=unit, description=description, color=color, groupId=group))
        return key

    def link(self, source, target, value, kind='retained'):
        if value:
            self.data['links'].append(dict(source=source, target=target, value=value, kind=kind))

    def finish(self):
        incoming, outgoing = Counter(), Counter()
        for link in self.data['links']:
            outgoing[link['source']] += link['value']
            incoming[link['target']] += link['value']
        for node in self.data['nodes']:
            for counts in (incoming, outgoing):
                if node['id'] in counts and counts[node['id']] != node['count']:
                    raise ValueError(f"Pipeline does not reconcile: {node['id']}")
        return self.data


def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        yield from csv.DictReader(handle)


def limited_evidence_reason(focus):
    """Same exclusive explanation order as the population-focus row."""
    fields = [focus.get(key) for key in ('dbPapers', 'mlPapers', 'dbPublicationYears', 'serviceYears')]
    if focus.get('label') != 'Limited evidence' or not all(isinstance(n, (int, float)) for n in fields):
        return None
    db, ml, years, service = fields
    if db + ml == 0:
        return 'no-papers'
    if db < 2 * ml:
        return 'ratio'
    if db < 2 or years < 2:
        return 'db-evidence'
    if service < 2:
        return 'pc-service'
    return None


def people_flow(data_dir, all_people, cohort, policy, group):
    flow = Flow('people', ['Service sources', 'Intake', 'Name grouping', 'Candidate lookup', 'DBLP matching',
                            'Identity merging', 'PC eligibility', 'Publication focus'])
    db_path = data_dir / 'entity_resolution.sqlite'
    with closing(sqlite3.connect(f'{db_path.as_uri()}?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        observations = [dict(r) for r in conn.execute('SELECT * FROM reviewer_observations')]
        names = [dict(r) for r in conn.execute('SELECT * FROM name_resolution')]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        duplicates = [dict(r) for r in conn.execute('SELECT * FROM technical_duplicate_log')] if 'technical_duplicate_log' in tables else []
        repairs = conn.execute("SELECT COUNT(*) FROM scraper_repair_log WHERE action='repair'").fetchone()[0] if 'scraper_repair_log' in tables else None
        profile_count = conn.execute('SELECT COUNT(DISTINCT dblp_pid) FROM dblp_candidates').fetchone()[0] if 'dblp_candidates' in tables else None
    traced = {(r.get('source_db'), r.get('source_rowid')) for r in observations}
    duplicate_refs = {(r['source_db'], r['source_rowid']) for r in duplicates}
    intake = Counter()
    sources, input_rows = [], []
    for path in sorted(data_dir.glob('*_reviewers.sqlite')):
        with closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'reviewer_records' not in tables:
                continue
            records = [dict(r) for r in conn.execute('SELECT rowid AS source_rowid,* FROM reviewer_records')]
            pages = [dict(r) for table in ('source_pages', 'source_documents') if table in tables
                     for r in conn.execute('SELECT * FROM ' + table)]
        by_year = defaultdict(list)
        for record in records:
            by_year[record.get('year', record.get('conference_year'))].append(record)
            if record.get('qualifies_db_seed', 1) not in (1, None):
                status = 'Outside seed roles'
            elif (path.name, record['source_rowid']) in traced:
                status = 'Ingested'
            elif (path.name, record['source_rowid']) in duplicate_refs:
                status = 'Technical duplicates'
            elif not record.get('name', '').strip():
                status = 'Empty name'
            else:
                status = 'Not traced to intake'
            intake[status] += 1
            input_rows.append((path.name, record, status))
        for year in sorted(set(by_year) | {p.get('year', p.get('conference_year')) for p in pages}, key=str):
            year_records = by_year[year]
            urls = sorted({p['url'] for p in pages if p.get('year', p.get('conference_year')) == year}
                          | {r['source_url'] for r in year_records if r.get('source_url')})
            sources.append(dict(venue=year_records[0].get('venue', path.stem) if year_records else path.stem.replace('_reviewers', '').upper(),
                                year=year, pages=len(urls), entries=len(year_records), urls=urls))
        if records:
            venue = records[0].get('venue', path.stem)
            urls = {u for s in sources if s['venue'] == venue for u in s['urls']}
            flow.node('source-'+path.stem, 'SIGMOD/PODS' if venue == 'SIGMOD' else venue, len(records), 0, 'parsed service entries',
                      f'{len(urls)} source documents across {len(by_year)} years. One person may have several entries.')
    flow.data['sources'] = sources
    # Old/minimal exports may not retain row provenance. Do not fabricate source flows.
    source_nodes = [n for n in flow.data['nodes'] if n['stage'] == 0]
    has_trace = bool(source_nodes) and intake['Ingested'] == len(observations)
    if not has_trace:
        flow.data['nodes'] = []
        flow.node('stored-observations', 'Stored observations', len(observations), 0, 'service entries', 'Source-to-intake row provenance is unavailable in this export.')
        flow.data['notes'].append('Source-to-intake counts cannot be reconciled in this export. The flow starts at stored service observations.')
    flow.node('intake', 'Stored observations', len(observations), 1, 'service entries',
              'Seed-role records after name repairs and exact technical duplicate removal.' + (f' {repairs} name repairs retained.' if repairs is not None else ''))
    if has_trace:
        for node in source_nodes:
            filename = node['id'].removeprefix('source-') + '.sqlite'
            own = [(r, s) for filename_, r, s in input_rows if filename_ == filename]
            counts = Counter(s for _, s in own)
            flow.link(node['id'], 'intake', counts['Ingested'])
            for status in counts:
                if status != 'Ingested':
                    flow.link(node['id'], 'intake-'+status, counts[status], 'excluded')
        for status, count in intake.items():
            if status != 'Ingested':
                flow.node('intake-'+status, status, count, 1, 'service entries', 'Source rows not forwarded to name grouping.', 'attention')
    else:
        flow.link('stored-observations', 'intake', len(observations))
    valid = [r for r in observations if r.get('name_is_valid', 1)]
    repeats = len(valid) - len({r['name_key'] for r in valid})
    flow.node('name-groups', 'Distinct name groups', len({r['name_key'] for r in valid}), 2, 'normalized name groups', 'Repeated service entries share one normalized name. Different names can still refer to the same person.')
    flow.link('intake', 'name-groups', len(valid)-repeats)
    if repeats:
        flow.node('repeat-service', 'Repeat service entries', repeats, 2, 'additional service entries', 'Collapsed when counting names. Service history is preserved; these are not rejected people.', 'muted')
        flow.link('intake', 'repeat-service', repeats, 'merged')
    if len(valid) < len(observations):
        flow.node('invalid-names', 'Invalid names', len(observations)-len(valid), 2, 'service entries', 'Names fail person-name validation.', 'attention')
        flow.link('intake', 'invalid-names', len(observations)-len(valid), 'excluded')
    categories = Counter()
    lookup = defaultdict(Counter)
    for row in names:
        category = ('matched' if row['resolved_dblp_pid'] else 'ambiguous' if row['status'].startswith('ambiguous')
                    else 'candidate' if row['all_candidate_count'] else 'unmatched')
        categories[category] += 1
        count = row['all_candidate_count']
        lookup['none' if not count else 'one' if count == 1 else 'several'][category] += 1
    for key, label, color, description in [
            ('one', 'One candidate', 'sage', 'One person profile was found. Its name and identity evidence still need to pass matching.'),
            ('several', 'Several candidates', 'sage', 'More than one profile was found. Name and affiliation evidence determine which can be used.'),
            ('none', 'No person candidate', 'attention', 'No usable DBLP person profile was found. Some names match only a disambiguation page.')]:
        detail = group('flow-lookup-'+key, label, [
            {'id': r['name_key'], 'label': r['representative_name'],
             'detail': f"{r['all_candidate_count']} indexed DBLP person candidates.",
             'url': 'https://dblp.org/pid/'+r['resolved_dblp_pid'] if r['resolved_dblp_pid'] else ''}
            for r in names if ('none' if not r['all_candidate_count'] else 'one' if r['all_candidate_count'] == 1 else 'several') == key
        ], color, description)
        flow.node('lookup-'+key, label, sum(lookup[key].values()), 3, 'name groups', description, color, detail['id'])
        flow.link('name-groups', 'lookup-'+key, sum(lookup[key].values()), 'review' if key == 'none' else 'retained')
        for category, count in lookup[key].items():
            flow.link('lookup-'+key, category, count, 'retained' if category == 'matched' else 'review')
    flow.data['candidateLookup'] = {'nameGroups': len(names), 'distinctProfiles': profile_count,
                                  'nameProfilePairs': sum(r['all_candidate_count'] for r in names)}
    if profile_count is not None:
        several = next(node for node in flow.data['nodes'] if node['id'] == 'lookup-several')
        several['details'] = [f'{profile_count:,} distinct DBLP profiles appear across all candidate lists. These are possible matches, not study members.']
    for key, label, color, description in [
            ('matched', 'Matched names', 'sage', 'Name groups assigned to a DBLP profile; matches can still need review.'),
            ('ambiguous', 'Ambiguous names', 'attention', 'The available evidence cannot identify one person confidently. This includes names with only a DBLP disambiguation page.'),
            ('candidate', 'Needs verification', 'attention', 'A candidate exists but has not passed the identity rules.'),
            ('unmatched', 'No candidate', 'attention', 'No local DBLP identity candidate was found.')]:
        flow.node(key, label, categories[key], 4, 'name groups', description, color, 'names-'+key)
    missing = len({r['name_key'] for r in valid}) - len(names)
    if missing:
        flow.node('no-decision', 'No identity decision', missing, 3, 'name groups', 'Name groups absent from the resolution table.', 'attention')
        flow.link('name-groups', 'no-decision', missing, 'excluded')
    pids = {r['resolved_dblp_pid'] for r in names if r['resolved_dblp_pid']}
    flow.node('identities', 'Unique researchers', len(pids), 5, 'DBLP identities', 'All names assigned to the same DBLP PID count as one researcher.')
    flow.link('matched', 'identities', len(pids))
    aliases = categories['matched'] - len(pids)
    if aliases:
        flow.node('aliases', 'Additional name aliases', aliases, 5, 'name groups', 'Merged into an existing DBLP identity, not rejected researchers.', 'muted')
        flow.link('matched', 'aliases', aliases, 'merged')
    people_by_pid = {p['dblpPid']: p for p in all_people}
    candidates = {p['dblpPid']: p for p in policy['candidates']}
    flow.node('pc', 'Qualifying PC members', len(candidates), 6, 'researchers', 'Selected DB venues, 2022–2025. Includes research, industry and demo PCs and program leadership; excludes Shadow PCs.')
    flow.link('identities', 'pc', len(candidates))
    outside = [p for pid, p in people_by_pid.items() if pid not in candidates]
    flow.node('outside-pc', 'Outside PC scope', len(outside), 6, 'researchers', 'Matched service records, but no qualifying PC role in the selected venues and years.', 'muted', 'people-4')
    flow.link('identities', 'outside-pc', len(outside), 'excluded')
    not_exported = pids - set(people_by_pid)
    if not_exported:
        flow.node('people-not-exported', 'Not in website input', len(not_exported), 6, 'DBLP identities', 'Resolved identities absent from the website person export.', 'attention')
        flow.link('identities', 'people-not-exported', len(not_exported), 'excluded')
    for i, label, color in [(0, 'DB-focused', 'accent'), (1, 'Limited evidence', 'attention'), (2, 'ML-focused', 'green'), (3, 'Mixed', 'dark-green')]:
        members = [p for p in candidates.values() if p['focus']['label'] == label]
        description = ('Included in the study. ' + str(sum(p['focus'].get('basis') == 'publication_and_service' for p in members)) + ' qualify through repeated PC service.' if i == 0
                       else 'Outside the study cohort. ' + ('Neither publication minimum nor repeated-service fallback is met.' if i == 1 else 'Publication balance does not meet the DB-focused rule.'))
        flow.node('focus-'+str(i), label, len(members), 7, 'researchers', description, color, 'people-'+str(i))
        flow.link('pc', 'focus-'+str(i), len(members), 'retained' if i == 0 else 'excluded')
    assert len(cohort) == len([p for p in candidates.values() if p['focus']['label'] == 'DB-focused'])
    flow.data['notes'] += ['Repeat entries and aliases are merged. Unmatched names need review; PC scope and publication focus are study rules.',
        'Counts start at saved source entries. Names missed by the scrapers are not measured.']
    flow.data['checks'] = {'nameRepairs': repairs, 'technicalDuplicates': len(duplicates), 'invalidNames': len(observations)-len(valid), 'outsideSeedRoles': intake['Outside seed roles'] if has_trace else None}
    return flow.finish()


def papers_flow(data_dir, all_works, cohort_works, cohort, group, row_issues):
    raw_path = data_dir / 'dblp_publications_for_db_people.csv'
    resolution_path = data_dir / 'publication_entity_resolution.csv'
    entities_path = data_dir / 'dblp_publication_entities_for_db_people.csv'
    if not raw_path.exists() or not resolution_path.exists():
        return {'id': 'papers', 'unavailable': 'Raw publication scan or version-resolution export is unavailable.'}
    raw, links, author_pids = {}, 0, set()
    for line, row in enumerate(rows(raw_path), 2):
        links += 1
        author_pids.add(row.get('dblp_pid', ''))
        raw.setdefault(row['dblp_key'].strip() or f'{raw_path.name}:line-{line}', row)
    resolutions = {r['dblp_key']: r for r in rows(resolution_path) if r['dblp_key'] in raw}
    if not all(r.get('work_id') for r in resolutions.values()):
        return {'id': 'papers', 'unavailable': 'The version export lacks work IDs needed to trace records into website works.'}
    flow = Flow('papers', ['Author matching', 'Accepted links', 'Record grouping', 'Version linking',
                           'Website input', 'Study authors', 'Analysis use'])
    cache_path = raw_path.with_suffix(raw_path.suffix + '.scan.json')
    scan = json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else {}
    verified = (scan.get('output_sha256') == hashlib.sha256(raw_path.read_bytes()).hexdigest()
                and scan.get('stats', {}).get('accepted') == links)
    stats = scan.get('stats', {}) if verified else {}
    ambiguity = stats.get('skipped_alias_ambiguity', 0) + stats.get('skipped_orcid_ambiguity', 0)
    flow.data['snapshot'] = {'id': scan.get('snapshot_id') if verified else None,
                             'xmlRecordsScanned': stats.get('records'), 'verified': verified}
    if verified and scan.get('target_fingerprint'):
        try:
            from .scan_dblp_publications import load_targets, target_fingerprint
        except ImportError:
            from scan_dblp_publications import load_targets, target_fingerprint
        targets = load_targets(data_dir / 'entity_resolution.sqlite')
        target_pids = {p.pid for p in targets}
        if scan['target_fingerprint'] == target_fingerprint(targets) and author_pids <= target_pids:
            flow.data['searchPopulation'] = {'searched': len(target_pids), 'withPublications': len(author_pids),
                                            'withoutPublications': len(target_pids - author_pids)}
    flow.node('candidates', 'Candidate author links', links+ambiguity, 0, 'researcher–record links', 'Candidate links staged while scanning DBLP. One record may involve several target researchers.')
    flow.node('accepted', 'Accepted author links', links, 1, 'researcher–record links', 'Author PID, verified ORCID, globally unique author signature or a reviewed paper-specific assignment passes the matching rules.')
    flow.link('candidates', 'accepted', links)
    if ambiguity:
        flow.node('ambiguous-authors', 'Ambiguous author links', ambiguity, 1, 'researcher–record links', 'Exact alias or ORCID has conflicting global owners.', 'attention')
        flow.link('candidates', 'ambiguous-authors', ambiguity, 'excluded')
    flow.node('records', 'Unique DBLP records', len(raw), 2, 'DBLP records', 'The same publication record is counted once across its researcher links.')
    flow.link('accepted', 'records', len(raw))
    if links > len(raw):
        flow.node('coauthors', 'Additional author links', links-len(raw), 2, 'researcher–record links', 'Retained coauthor links to records already counted once. This is a change of counting unit, not lost authorship evidence.', 'muted')
        flow.link('accepted', 'coauthors', links-len(raw), 'merged')
    work_ids = {r['work_id'] for r in resolutions.values()}
    flow.node('works', 'Distinct works', len(work_ids), 3, 'deduplicated works', 'A preprint and its linked published version count as one work.')
    flow.link('records', 'works', len(work_ids))
    extra_versions = len(resolutions)-len(work_ids)
    if extra_versions:
        flow.node('versions', 'Linked versions', extra_versions, 3, 'additional DBLP records', 'Kept as version evidence, not counted as additional works.', 'muted', 'papers-linked')
        flow.link('records', 'versions', extra_versions, 'merged')
    unresolved = len(raw)-len(resolutions)
    if unresolved:
        flow.node('undecided', 'No version decision', unresolved, 3, 'DBLP records', 'Source records absent from the version-resolution export.', 'attention', 'papers-unresolved')
        flow.link('records', 'undecided', unresolved, 'excluded')
    # Set intersection prevents a count coincidence from hiding missing/extra work IDs.
    retained = work_ids & set(all_works)
    flow.node('website', 'Website works', len(all_works), 4, 'distinct works', 'Works retained after required title, year and key validation for matched researchers.')
    flow.link('works', 'website', len(retained))
    skipped = work_ids - set(all_works)
    if skipped:
        bad = set()
        if entities_path.exists():
            copies = defaultdict(list)
            for row in rows(entities_path):
                if row.get('work_id') in skipped:
                    copies[row['work_id']].append(row)
            bad = {key for key, records in copies.items() if all(row_issues(r) for r in records)}
        for key, keys, label, description in [('invalid-work', bad, 'Invalid display metadata', 'All available website copies fail required-field validation.'),
                ('missing-work', skipped-bad, 'Not in website input', 'No retained website copy; input or person linkage needs investigation.')]:
            if keys:
                detail = group('flow-'+key, label, [{'id': k, 'label': k, 'detail': description} for k in keys], 'attention', description)
                flow.node(key, label, len(keys), 4, 'distinct works', description, 'attention', detail['id'])
                flow.link('works', key, len(keys), 'excluded')
    extras = set(all_works) - work_ids
    if extras:
        flow.node('untraced-work', 'Untraced website works', len(extras), 3, 'distinct works', 'Website work IDs absent from the source resolution export. Check artifact consistency.', 'attention')
        flow.link('untraced-work', 'website', len(extras))
    flow.node('cohort', 'Study-cohort works', len(cohort_works), 5, 'distinct works', 'At least one author is a DB-focused qualifying PC member.')
    flow.link('website', 'cohort', len(cohort_works))
    outside = len(all_works)-len(cohort_works)
    flow.node('outside', 'Outside study cohort', outside, 5, 'distinct works', 'No included author. These publications are retained for population classification and excluded-profile context.', 'muted', 'works-outside')
    flow.link('website', 'outside', outside, 'excluded')
    cohort_ids = {p['dblpPid'] for p in cohort}
    audit_path = data_dir / 'reports/focus_publication_audit.csv'
    accepted_db, uncertain_db = set(), set()
    if audit_path.exists():
        for row in rows(audit_path):
            if row['pid'] not in cohort_ids:
                continue
            if row.get('counted', '').casefold() == 'true':
                accepted_db.add(row.get('work_id'))
            elif row.get('reason') in ('Full-paper length cannot be verified', 'Missing snapshot metadata'):
                uncertain_db.add(row.get('work_id'))
    main = {k for k, ps in cohort_works.items() if any(p['scope'] == 'main' for p in ps)}
    db = (accepted_db & set(cohort_works)) - main
    uncertain = (uncertain_db & set(cohort_works)) - main - db
    other = set(cohort_works) - main - db - uncertain
    for key, label, keys, color, description, existing_group in [
            ('main', 'Main ML charts', main, 'accent', 'Main-conference works at NeurIPS, ICML or ICLR involving the study cohort.', 'works-main'),
            ('db', 'DB-focus evidence', db, 'database', 'Full selected DB conference works used by the DB-focused classifier. Also available in profile context.', None),
            ('uncertain', 'Unverified DB evidence', uncertain, 'attention', 'Retained in profiles, excluded from DB-focus evidence: full-paper length or snapshot metadata is unverified.', None),
            ('context', 'Other profile works', other, 'sage', 'Other retained publications. Workshops, journals and other context are not missing papers.', None)]:
        if not existing_group:
            detail = group('flow-works-'+key, label, [{'id': k, 'label': cohort_works[k][0]['title'],
                'detail': description, 'url': cohort_works[k][0].get('url', '')} for k in keys], color, description)
            existing_group = detail['id']
        flow.node(key, label, len(keys), 6, 'distinct works', description, color, existing_group)
        flow.link('cohort', key, len(keys), 'retained' if key != 'uncertain' else 'review')
    flow.data['notes'] = ['Counting units change from author links to DBLP records to distinct works. Coauthor links and linked versions remain available; they are not lost papers.',
                          'Outside-cohort works and profile context are retained. Unverified DB evidence and open display-category reviews are separate checks.']
    if not audit_path.exists():
        flow.data['notes'].append('DB-focus paper audit is unavailable; DB evidence cannot be separated from profile context.')
    if not verified:
        flow.data['notes'].append('Scan log is unavailable or does not match the raw CSV. Pre-acceptance author ambiguity is not measured; the flow starts with accepted links.')
        flow.data['nodes'][0].update(label='Collected author links', description='Accepted links in the raw CSV. Rejected author candidates are not measured in this export.')
    flow.data['checks'] = {'authorAmbiguity': ambiguity if verified else None,
                            'invalidWebsiteWorks': len(skipped), 'unresolvedRecords': unresolved}
    return flow.finish()
