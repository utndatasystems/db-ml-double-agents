"""Independent read-only reconciliation of saved browser and coverage assets."""
import collections
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parents[3]
web = root/'website'
data = root/'data'
out = {'failures': []}
def check(value, label):
    if not value:
        out['failures'].append(label)
def read_records(descriptor):
    path = web/descriptor['detailUrl']
    payload = path.read_bytes()
    check(hashlib.sha256(payload).hexdigest()[:12] in path.name, 'coverage hash '+descriptor['id'])
    records = json.loads(payload)['records']
    check(len(records) == descriptor['count'], 'coverage count '+descriptor['id'])
    check(len({r['id'] for r in records}) == len(records), 'coverage unique IDs '+descriptor['id'])
    return records

coverage = json.loads((web/'data/coverage_summary.json').read_text(encoding='utf-8'))
descriptors = {}
def collect(value):
    if isinstance(value, dict):
        if value.get('detailUrl') and 'count' in value:
            descriptors[value['id']] = value
        for child in value.values():
            collect(child)
    elif isinstance(value, list):
        for child in value:
            collect(child)
collect(coverage)
out['coverageDescriptors'] = len(descriptors)
for descriptor in descriptors.values():
    read_records(descriptor)
out['flowComponents'] = {}
for kind in ('people', 'papers'):
    flow = coverage['flows'][kind]
    nodes = {n['id']: n for n in flow['nodes']}
    counts = {}
    for node in nodes.values():
        for side in ('source', 'target'):
            values = [link['value'] for link in flow['links'] if link[side] == node['id']]
            check(not values or sum(values) == node['count'], kind+' '+node['id']+' '+side+' conservation')
        descriptor = descriptors[node['groupId']]
        check(descriptor.get('representedCount', descriptor['count']) == node['count'], kind+' node '+node['id'])
        check(descriptor['unit'] == node['unit'], kind+' unit '+node['id'])
        counts[node['id']] = node['count']
    for link in flow['links']:
        descriptor = descriptors[link['groupId']]
        check(descriptor.get('representedCount', descriptor['count']) == link['value'], kind+' edge '+link['groupId'])
        check(descriptor['unit'] == nodes[link['target']]['unit'], kind+' edge unit '+link['groupId'])
    out['flowComponents'][kind] = {'nodes': len(nodes), 'links': len(flow['links']), 'counts': counts}

browser = json.loads((web/'data/site_data.js').read_text(encoding='utf-8').removeprefix('window.CROSSOVER_DATA=').strip().removesuffix(';'))
all_people = browser['people'] + browser['additionalExplorerPeople']
check(len({p['id'] for p in all_people}) == len(all_people), 'browser duplicate person')
search = json.loads((web/browser['meta']['publicationSearchUrl']).read_text(encoding='utf-8'))['people']
eager_count = deferred_count = 0
main_works, side_works, authored_pairs = set(), set(), set()
for person in all_people:
    ml = person['ml']
    eager = ml['papers']
    deferred = []
    if ml.get('detailUrl'):
        path = web/ml['detailUrl']
        content = path.read_text(encoding='utf-8')
        check(hashlib.sha256(content.rstrip('\n').encode()).hexdigest()[:12] in path.name, 'person hash '+person['id'])
        detail = json.loads(content)
        check(detail['personId'] == person['id'], 'lazy ownership '+person['id'])
        deferred = detail['papers']
        check(detail['paperCount'] == len(deferred), 'lazy declared paperCount '+person['id'])
    check(len(eager) == ml['mainPaperCount'], 'main count '+person['id'])
    check(len(deferred) == ml['sidePaperCount'] == ml['deferredPaperCount'], 'side count '+person['id'])
    check(len(eager) + len(deferred) == ml['paperCount'], 'total count '+person['id'])
    check(all(p['scope'] == 'main' for p in eager), 'eager scope '+person['id'])
    check(all(p['scope'] != 'main' for p in deferred), 'deferred scope '+person['id'])
    check(len({p['workId'] for p in eager+deferred}) == ml['paperCount'], 'duplicate work '+person['id'])
    check(search.get(person['id'], '') == '\n'.join(p['title'].strip() for p in deferred if p['title'].strip()), 'search titles '+person['id'])
    eager_count += len(eager)
    deferred_count += len(deferred)
    if person in browser['people']:
        main_works.update(p['workId'] for p in eager)
        side_works.update(p['workId'] for p in deferred)
        authored_pairs.update((person['dblpPid'], p['workId']) for p in eager)
summary = browser['summary']
check(len(main_works) == summary['uniqueMainPapers'], 'summary main works')
check(len(side_works) == summary['uniqueSidePapers'], 'summary side works')
out['browser'] = {'cohortPeople': len(browser['people']), 'additionalPeople': len(browser['additionalExplorerPeople']), 'eagerPapersAllPc': eager_count, 'deferredPapersAllPc': deferred_count, 'mainWorks': len(main_works), 'sideWorks': len(side_works), 'mainAuthorships': len(authored_pairs)}

with sqlite3.connect('file:'+str(data/'entity_resolution.sqlite')+'?mode=ro', uri=True) as conn:
    names = {r[0]: r[1] for r in conn.execute('select name_key, resolved_dblp_pid from name_resolution')}
    decisions = {r[0]: r[1] for r in conn.execute('select name_key, status from name_resolution')}
    observations = conn.execute('select count(*) from reviewer_observations').fetchone()[0]
check(coverage['collection']['serviceObservations'] == observations, 'stored observations')
for category in ('matched', 'ambiguous', 'candidate', 'unmatched'):
    actual = {r['id'] for r in read_records(descriptors['names-'+category])}
    if category == 'matched':
        expected = {name for name, pid in names.items() if pid}
        check(actual == expected, 'matched name exact subset')
out['sourceComparison'] = {'observations': observations, 'nameGroups': len(names), 'resolvedPids': len(set(filter(None, names.values())))}

raw_records = set()
raw_links = 0
with (data/'dblp_publications_for_db_people.csv').open(encoding='utf-8-sig', newline='') as handle:
    for row in csv.DictReader(handle):
        raw_links += 1
        raw_records.add(row['dblp_key'])
flow = coverage['flows']['papers']
record_node = next(n for n in flow['nodes'] if n['id'] == 'records')
check({r['id'] for r in read_records(descriptors[record_node['groupId']])} == raw_records, 'raw DBLP record exact subset')
out['rawComparison'] = {'links': raw_links, 'records': len(raw_records)}
(root/'data/reports/round2/exports_evidence.json').write_text(json.dumps(out, indent=2)+'\n', encoding='utf-8')
print(json.dumps(out))
raise SystemExit(bool(out['failures']))
