import collections, csv, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
import resolve_publication_entities as r
from test_publication_entity_resolution import row

def publication(key, title, pid='p1', rels=None):
    return row(pid=pid, key=key, title=title, year=2024,
               authors=['Alice Example'], journal='', record_type='inproceedings',
               publtype='', venue_label='SIGMOD', rels=rels)

preprints = [row(pid='p1', key=f'journals/corr/abs-2401-0000{i}', title=f'Preprint {i}',
                 year=2024, authors=['Alice Example'],
                 rels=[{'type':'versionOf','target':'abstract/shared'}]) for i in (1, 2)]
fixture = preprints + [publication('conf/sigmod/A24', 'Article A'), publication('conf/sigmod/B24', 'Article B')]
metadata = {'2401.00001': {'title':'Article A', 'doi':'10.1000/a', 'status':'found'},
            '2401.00002': {'title':'Article B', 'doi':'10.1000/b', 'status':'found'}}
for rr, doi in zip(fixture[2:], ['10.1000/a','10.1000/b']): rr['ees_json']=json.dumps(['https://doi.org/'+doi])
fixture_result = r.resolve_rows(fixture, arxiv_metadata=metadata)

native_fixture = [row(pid='p1', key='journals/corr/abs-2401-00003', title='Article B', year=2024,
                 authors=['Alice Example'], rels=[{'type':'versionOf','target':'conf/sigmod/A24'}]),
                 publication('conf/sigmod/A24', 'Article A'), publication('conf/sigmod/B24', 'Article B')]
native_result = r.resolve_rows(native_fixture)

rows, fields = r.load_rows(ROOT/'data'/r.RAW_PUBLICATIONS_CSV)
records = r.unique_records(rows)
cache = r.load_arxiv_metadata_cache(ROOT/'data/reports'/r.ARXIV_CACHE_JSON)
components,bases = r.build_components(records, cache)
multi = []
for root, keys in components.items():
    published = [k for k in keys if records[k].is_archival_publication]
    if len(published)>1:
        multi.append({'keys':keys, 'archivalKeys': published, 'bases':sorted(bases[root]),
                      'titles': {k:records[k].title for k in keys}})
owned=[]
with (ROOT/'data'/r.OUTPUT_CSV).open(encoding='utf-8-sig',newline='') as f:
    for rr in csv.DictReader(f):
        person=set(json.loads(rr['person_version_dblp_keys']))
        allkeys=set(json.loads(rr['version_dblp_keys']))
        if person != allkeys:
            owned.append({k:rr[k] for k in ['dblp_pid','canonical_name','dblp_key','year','scope','venue','author_position','author_count','work_id','person_version_dblp_keys','version_dblp_keys']})
out={'rawRows':len(rows),'uniqueRecords':len(records),'workEntities':len(components),
     'multiArchivalComponents':multi,'partialOwnershipRows':owned,
     'arxivStaleRootsFixture': {'stats':fixture_result[3], 'mapping':fixture_result[1]},
     'nativeBridgeFixture': {'stats':native_result[3], 'mapping':native_result[1]}}
dest=ROOT/'data/reports/round2/work_dedup_evidence.json'
dest.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'rawRows':len(rows),'uniqueRecords':len(records),'workEntities':len(components),'multiArchivalComponents':len(multi),'partialOwnershipRows':len(owned), 'arxivFixtureWorks':fixture_result[3]['work_entities'],'nativeFixtureWorks':native_result[3]['work_entities'],'output':str(dest)}))
