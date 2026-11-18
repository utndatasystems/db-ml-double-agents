import csv, json, sys, tempfile
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from test_publication_entity_resolution import row
from scripts.resolve_publication_entities import resolve_rows,write_csv
from scripts.build_site_data import load_publications,browser_payload_and_detail_files
from scripts.focus_population import apply_focus_population
rows=[]
metadata={}
for i in (0,1):
    pre=f'journals/corr/abs-2{i}01-00001'
    final=f'conf/icde/Final{i}'
    for pid,pos,name in [('p1',1,'Alice Example'),('p2',2,'Bob Example')]:
        rr=row(pid=pid,key=pre,title=f'Preprint {i}',year=2020+i,
               authors=['Alice Example','Bob Example'],
               rels=[{'type':'versionOf','target':final}])
        rr.update(author_position=str(pos),author_count='2',author_signature=name)
        rows.append(rr)
    rr=row(pid='p2',key=final,title=f'Final {i}',year=2021+i,
           authors=['Bob Example'],journal='',record_type='inproceedings',publtype='',venue_label='ICDE')
    rr.update(booktitle='ICDE',author_position='1',author_count='1',author_signature='Bob Example')
    rows.append(rr)
    metadata[final]={'pages':'1-12'}
resolved,_,_,_=resolve_rows(rows)
with tempfile.TemporaryDirectory(dir=ROOT/'data/reports/round2') as tmp:
    root=Path(tmp)
    path=root/'entities.csv'
    write_csv(path,resolved,list(dict.fromkeys(k for rr in resolved for k in rr)))
    loaded,_,_=load_publications(path,{'p1','p2'})
    people=[{'id':pid,'dblpPid':pid,'name':pid,'ml':{'papers':list(value['papers'].values())},
             'db':{'events':[{'venue':'ICDE','role':'program_committee_member','year':year} for year in (2022,2023)]}}
            for pid,value in loaded.items()]
    with patch('scripts.focus_population.read_metadata',return_value=metadata):
        included,policy=apply_focus_population(people,root)
    browser,details,_=browser_payload_and_detail_files({'people':included},[p for p in people if p not in included])
    decoded=[json.loads(text) for text in details.values()]
    by_pid={p['dblpPid']:p for p in people}
    assert by_pid['p1']['focus']['dbPapers']==0
    assert by_pid['p2']['focus']['dbPapers']==2
    assert len(included)==1
    for detail in decoded:
        pid=detail['personId']
        for paper in detail['papers']:
            assert paper['dblpKey'] in paper['personVersionDblpKeys']
            if pid=='p1':
                assert paper['dblpKey'].startswith('journals/corr/')
                assert paper['authorPosition']==0 and paper['authorCount']==2
                assert len(paper['resolvedVersions'])==2 and len(paper['personVersionDblpKeys'])==1
            else:
                assert paper['dblpKey'].startswith('conf/icde/')
                assert paper['authorPosition']==0 and paper['authorCount']==1
    result={'path':'raw author rows -> resolver -> canonical CSV -> loader -> focus -> eager/additional browser split -> lazy JSON roundtrip',
            'profiles':{p['dblpPid']:p['focus'] for p in people},'detailFiles':len(details),
            'additionalPeople':len(browser['additionalExplorerPeople']),'passed':True,
            'positionsAndYearsPreserved':True}
    (ROOT/'data/reports/round2/work_ownership_export_verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
