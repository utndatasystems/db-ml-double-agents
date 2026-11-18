import collections
import dataclasses
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import scrape_sigmod_reviewers as sigmod
import scrape_vldb_reviewers as vldb

HERE = Path(__file__).resolve().parent
def ro(name):
    conn = sqlite3.connect((ROOT / 'data' / name).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn
def page(year, kind):
    path = HERE / f'{year}.sigmod.org_org_{kind}.shtml.html'
    return path.read_bytes()
def key(r):
    return (r.year, r.role, r.review_round, r.name_key, r.affiliation)

conn = ro('sigmod_reviewers.sqlite')
out = {'official_pages': [], 'pods': {}, 'same_name_loss': {}}
for path in HERE.glob('*.shtml.html'):
    out['official_pages'].append({'path': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
for year in (2022, 2024):
    officers = sigmod.parse_sigmod_officers_page(page(year, 'conference_officers'), year,
        f'https://{year}.sigmod.org/org_conference_officers.shtml', 'audit')
    pods = sigmod.parse_sigmod_track_pc_page(page(year, 'pods_pc'), year=year,
        source_url=f'https://{year}.sigmod.org/org_pods_pc.shtml', fetched_at='audit', page_kind='pods')
    incoming = sigmod.merge_sigmod_records(officers, pods)
    incoming_keys = set(map(key, incoming))
    stored = [dict(r) for r in conn.execute('SELECT * FROM reviewer_records WHERE year=? AND (source_url LIKE ? OR source_url LIKE ?)',
        (year, '%org_conference_officers.shtml', '%org_pods_pc.shtml'))]
    stored_keys = {(r['year'], r['role'], r['review_round'], r['name_key'], r['affiliation']) for r in stored}
    out['pods'][year] = {'new_rows': [dataclasses.asdict(r) for r in incoming if key(r) not in stored_keys],
        'missing_from_current_parse': [r for r in stored if (r['year'],r['role'],r['review_round'],r['name_key'],r['affiliation']) not in incoming_keys],
        'stored_source_pages': [dict(r) for r in conn.execute('SELECT * FROM source_pages WHERE year=?', (year,))]}

parsed = sigmod.parse_sigmod_pc_page(page(2025, 'sigmod_pc'), 2025,
    'https://2025.sigmod.org/org_sigmod_pc.shtml', 'audit')
merged = sigmod.merge_sigmod_records(parsed)
out['same_name_loss'] = {
    'parsed_count': len(parsed), 'merged_count': len(merged),
    'lost': [dataclasses.asdict(r) for r in parsed if key(r) not in set(map(key, merged))],
    'stored_chao': [dict(r) for r in conn.execute('SELECT year,role,name,affiliation,source_url FROM reviewer_records WHERE name_key=?', ('chao zhang',))],
    'duplicate_role_names': {str(k): [dataclasses.asdict(r) for r in parsed if (r.role,r.review_round,r.name_key)==k]
        for k,n in collections.Counter((r.role,r.review_round,r.name_key) for r in parsed).items() if n>1}
}
entity = ro('entity_resolution.sqlite')
out['tables'] = [r[0] for r in entity.execute("SELECT name FROM sqlite_master WHERE type='table'")]
out['chao_namegroup'] = [dict(r) for r in entity.execute('SELECT * FROM name_groups WHERE name_key=?', ('chao zhang',))]
out['chao_decision'] = [dict(r) for r in entity.execute('SELECT * FROM name_resolution WHERE name_key=?', ('chao zhang',))]
out['jia_decision'] = [dict(r) for r in entity.execute('SELECT * FROM name_resolution WHERE name_key=?', ('jia yu',))]
out['sigmod_research_comparisons'] = {}
for year in (2022, 2024, 2025):
    rows = sigmod.parse_sigmod_pc_page(page(year, 'sigmod_pc'), year, f'https://{year}.sigmod.org/org_sigmod_pc.shtml', 'audit')
    kept = sigmod.merge_sigmod_records(rows)
    keys = set(map(key, kept))
    stored_keys = {(r['year'], r['role'], r['review_round'], r['name_key'], r['affiliation'])
        for r in conn.execute('SELECT * FROM reviewer_records WHERE year=? AND source_url LIKE ?', (year,'%org_sigmod_pc.shtml'))}
    out['sigmod_research_comparisons'][year] = {'parsed':len(rows), 'merged':len(kept),
        'lost_by_merge':[dataclasses.asdict(r) for r in rows if key(r) not in keys],
        'source_rows_missing_from_database':[dataclasses.asdict(r) for r in rows if key(r) not in stored_keys]}
fixture = b'<h1>Organization: SIGMOD Program Committee</h1><p>Program Chairs</p><p>Example Chair, Example University</p><p>PC members</p><p>Chao Zhang, University of Waterloo</p><p>Chao Zhang, Tsinghua University</p>'
fixture_rows = sigmod.parse_sigmod_pc_page(fixture,2025,'https://example.test','audit')
out['boundary_fixture'] = {'parsed':len(fixture_rows),'merged':len(sigmod.merge_sigmod_records(fixture_rows)),'expected_merge':3}
vl_rows = vldb.parse_vldb_review_board_page((HERE/'www.vldb.org_pvldb_volumes_18_review-board.html').read_bytes(),
    conference_year=2025,expected_volume=18,source_url='https://www.vldb.org/pvldb/volumes/18/review-board',fetched_at='audit')
vl_conn=ro('vldb_reviewers.sqlite')
vl_key=lambda r:(r.conference_year,r.role,r.name_key,r.affiliation)
vl_stored={(r['conference_year'],r['role'],r['name_key'],r['affiliation']) for r in vl_conn.execute('SELECT * FROM reviewer_records WHERE conference_year=2025 AND source_url LIKE ?',('%review-board',))}
out['vldb_2025_review_board']={'parsed':len(vl_rows),'stored':len(vl_stored),'source_rows_missing':[dataclasses.asdict(r) for r in vl_rows if vl_key(r) not in vl_stored]}
# Counterfactual only: replay selected fixed-source observations through the
# corrected merge. Production databases remain read-only.
plans={}
for year in (2022,2024,2025):
    stored=[sigmod.ReviewerRecord(**{f.name:row[f.name] for f in dataclasses.fields(sigmod.ReviewerRecord)})
        for row in conn.execute('SELECT * FROM reviewer_records WHERE year=? ORDER BY id',(year,))]
    fresh=[]
    source_urls=set()
    research_url=f'https://{year}.sigmod.org/org_sigmod_pc.shtml'
    source_urls.add(research_url)
    fresh+=sigmod.parse_sigmod_pc_page(page(year,'sigmod_pc'),year,research_url,'audit')
    if year in (2022,2024):
        officer_url=f'https://{year}.sigmod.org/org_conference_officers.shtml'
        pods_url=f'https://{year}.sigmod.org/org_pods_pc.shtml'
        source_urls.update((officer_url,pods_url))
        fresh+=sigmod.parse_sigmod_officers_page(page(year,'conference_officers'),year,officer_url,'audit')
        fresh+=sigmod.parse_sigmod_track_pc_page(page(year,'pods_pc'),year=year,source_url=pods_url,fetched_at='audit',page_kind='pods')
    rows=sigmod.merge_sigmod_records([r for r in stored if r.source_url not in source_urls]+fresh)
    before=set(map(key,stored)); after=set(map(key,rows))
    plans[year]={'before_count':len(stored),'after_count':len(rows),
        'source_urls_reparsed':sorted(source_urls),'added':[dataclasses.asdict(r) for r in rows if key(r) not in before],
        'removed':[dataclasses.asdict(r) for r in stored if key(r) not in after]}
    (HERE/f'sigmod_{year}_planned_records.json').write_text(json.dumps([dataclasses.asdict(r) for r in rows],indent=2,ensure_ascii=False),encoding='utf-8')
out['planned_fixed_source_deltas']=plans
(HERE / 'source_coverage_evidence_after_fix.json').write_text(json.dumps(out, indent=2,ensure_ascii=False), encoding='utf-8')
print(json.dumps({'boundary_fixture':out['boundary_fixture'],'plans':plans},indent=2,ensure_ascii=True))
