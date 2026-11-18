import csv, gc, importlib.util, io, json, subprocess, sys, types, unittest
from contextlib import redirect_stderr
from pathlib import Path
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT/'scripts'))
sys.path.insert(0, str(ROOT/'tests'))
import resolve_publication_entities as fixed
import test_publication_entity_resolution as tests
baseline = types.ModuleType('baseline_work_resolver')
sys.modules[baseline.__name__] = baseline
source=subprocess.check_output(['git','show','567b35b:scripts/resolve_publication_entities.py'],cwd=ROOT,text=True)
exec(compile(source,'567b35b:scripts/resolve_publication_entities.py','exec'),baseline.__dict__)
names=['test_title_fallbacks_cannot_bridge_native_archival_components',
       'test_arxiv_links_recheck_archival_membership_after_root_changes',
       'test_native_evidence_can_still_merge_archival_records']
tests.resolve_rows=baseline.resolve_rows
log=io.StringIO()
result=unittest.TextTestRunner(stream=log).run(unittest.TestSuite(tests.PublicationEntityResolutionTests(name) for name in names))
tests.resolve_rows=fixed.resolve_rows
keys=['dblp_pid','dblp_key','year','scope','venue','author_signature','author_position','author_count',
      'work_id','canonical_dblp_key','version_dblp_keys','person_version_dblp_keys']
def signature(rows):
    return {(r['dblp_pid'],r['work_id']):tuple(r.get(k,'') for k in keys) for r in rows}
rows,_=fixed.load_rows(ROOT/'data'/fixed.RAW_PUBLICATIONS_CSV)
cache=fixed.load_arxiv_metadata_cache(ROOT/'data/reports'/fixed.ARXIV_CACHE_JSON)
before=baseline.resolve_rows(rows,cache)
before_signature=signature(before[0]); before_stats=before[3]
del before
gc.collect()
after=fixed.resolve_rows(rows,cache)
after_signature=signature(after[0])
with (ROOT/'data'/fixed.OUTPUT_CSV).open(encoding='utf-8-sig',newline='') as f:
    stored=signature(list(csv.DictReader(f)))
out={'baselineCommit':'567b35b','baselineRegressionTestsRun':result.testsRun,
     'baselineRegressionFailures':len(result.failures),'baselineRegressionErrors':len(result.errors),
     'baselineRegressionLog':log.getvalue(),
     'beforeStats':before_stats,'afterStats':after[3],
     'sameFixedInputSelectedRows':before_signature==after_signature,
     'storedSelectedRowsAgree':stored==after_signature,
     'comparedFields':keys,'comparedRows':len(after_signature)}
destination=ROOT/'data/reports/round2/work_dedup_verification.json'
destination.write_text(json.dumps(out,indent=2),encoding='utf-8')
print(json.dumps({k:v for k,v in out.items() if k not in ['baselineRegressionLog','beforeStats','afterStats','comparedFields']}))
