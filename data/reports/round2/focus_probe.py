import csv, json, re, sys, tempfile, copy
from unittest.mock import patch
from pathlib import Path
from collections import Counter, defaultdict
ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from scripts.focus_population import page_count, db_paper_decision, publication_profile

def compare():
    from scripts.build_site_data import load_publications
    from scripts.focus_population import apply_focus_population, exclusions
    site=json.loads((ROOT/'website/data/site_data.js').read_text(encoding='utf-8').split('=',1)[1].strip().removesuffix(';'))
    people=copy.deepcopy(site['populationFocus']['candidates'])
    for p in people:p['id']='dblp:'+p['dblpPid']
    pubs,_,_=load_publications(ROOT/'data/dblp_publication_entities_for_db_people.csv',{p['dblpPid'] for p in people})
    titles=['Data-driven Visual Query Interfaces for Graphs: Past, Present, and (Near) Future.',
        'An Introduction to Federated Computation.',
        'Explainable AI: Foundations, Applications, Opportunities for Data Management Research.',
        'Responsible Data Integration: Next-generation Challenges.',
        'Multi-Tenant Cloud Data Services: State-of-the-Art, Challenges and Opportunities.',
        'Spatial Data Quality in the IoT Era: Management and Exploitation.',
        'HTAP Databases: What is New and What is Next.',
        'Dissecting, Designing, and Optimizing LSM-based Data Stores.']
    meta=json.loads((ROOT/'data/reports/focus_snapshot_metadata.json').read_text())['records']
    selected={}
    for p in people:
        p['ml']={'papers':list(pubs.get(p['dblpPid'],{'papers':{}})['papers'].values())}
        for work in p['ml']['papers']:
            for v in [work,*work.get('resolvedVersions',[])]:
                if (v.get('year')==2022 and v['title'].casefold() in {t.casefold() for t in titles} and v['dblpKey'].startswith('conf/sigmod/')) or v['dblpKey']=='conf/sigmod/PeiHTL08':
                    selected[v['dblpKey']]={'key':v['dblpKey'],'title':v['title'],'pages':meta.get(v['dblpKey'],{}).get('pages'),'decision':db_paper_decision(v,meta.get(v['dblpKey']))}
    print(json.dumps(list(selected.values()),indent=2,ensure_ascii=False))
    reviews=[r for r in exclusions() if r.get('verified_on')=='2026-11-07']
    old_exclusions=tuple(r for r in exclusions() if r not in reviews)
    baseline={p['dblpPid']:copy.deepcopy(p['focus']) for p in people}
    with tempfile.TemporaryDirectory() as directory,patch('scripts.focus_population.read_metadata',return_value=meta):
        with patch('scripts.focus_population.exclusions',return_value=old_exclusions):
            _,before=apply_focus_population(people,Path(directory))
        parity=[p['dblpPid'] for p in people if p['focus']!=baseline[p['dblpPid']]]
        before_profiles={p['dblpPid']:copy.deepcopy(p['focus']) for p in people}
        _,after=apply_focus_population(people,Path(directory))
    changed=[{'pid':p['dblpPid'],'name':p['name'],'before':before_profiles[p['dblpPid']],'after':p['focus']} for p in people if p['focus']!=before_profiles[p['dblpPid']]]
    audit=list(csv.DictReader((ROOT/'data/reports/focus_publication_audit.csv').open(encoding='utf-8-sig')))
    affected=[r for r in audit if r['key'] in selected and r['counted']=='True']
    result={'official_source':'https://2022.sigmod.org/sigmod_tutorials.shtml','reviews':reviews,'selected':selected,'baseline_parity_changes':parity,
        'before_groups':before['groups'],'after_groups':after['groups'],'counted_rows':len(affected),'counted_keys':len({r['key'] for r in affected}),
        'affected_people':len({r['pid'] for r in affected}),'changed_profiles':changed}
    (ROOT/'data/reports/round2/focus_evidence.json').write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['changed_profiles','selected','reviews']},indent=2))
    print('CLASSIFICATION_CHANGES',json.dumps([r for r in changed if r['before']['label']!=r['after']['label']],indent=2))

def main():
    audit=list(csv.DictReader((ROOT/'data/reports/focus_publication_audit.csv').open(encoding='utf-8-sig')))
    true=[r for r in audit if r['counted']=='True']
    uniq={r['key']:r for r in true}
    suspicious=[r for r in uniq.values() if re.search(r'tutorial|demonstrat|keynote|panel|invited|editorial|competition|challenge|contest|reproduc|benchmark report|welcome|foreword|preface|erratum',r['title'],re.I)]
    site=json.loads((ROOT/'website/data/site_data.js').read_text(encoding='utf-8').split('=',1)[1].strip().removesuffix(';'))
    pol=site['populationFocus']
    print('AUDIT',len(audit),len(true),len(uniq),'SUSPICIOUS',len(suspicious))
    for r in suspicious: print(json.dumps({k:r[k] for k in ['key','title','pages']},ensure_ascii=False))
    print('NEAR_BOUNDARIES')
    for p in pol['candidates']:
        f=p['focus'];D,M,Y=f['dbPapers'],f['mlPapers'],f['publicationYears']
        if ((D==2*M and D and D<12) or (D+M in [4,5] and Y in [2,3]) or f['basis']=='publication_and_service'):
            print(json.dumps({'pid':p['dblpPid'],'name':p['name'],**f},ensure_ascii=False))
    print('PUBTYPES',Counter(r['publtype'] for r in json.loads((ROOT/'data/reports/focus_snapshot_metadata.json').read_text())['records'].values()))

if __name__=='__main__':compare() if '--compare' in sys.argv else main()
