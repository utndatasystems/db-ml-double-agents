import csv
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from scripts.data_coverage import export_coverage, audit_publication_input, length_review_impact
from scripts.build_site_data import load_publications


def write_rows(path, rows):
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


class CoverageTests(unittest.TestCase):
    def test_every_flow_component_has_an_exact_evidence_list_and_split_edges_are_subsets(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':str(int(key!='r4')),'resolution_basis':'single_record' if key!='r4' else 'explicit_version'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2')]])
            summary=export_coverage(data,output,people,cohort,policy)
            descriptors={g['id']:g for g in summary['flows']['groups']}
            def read(component):
                descriptor=descriptors[component['groupId']]
                records=json.loads((output/Path(descriptor['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))['records']
                self.assertEqual(len(records),component.get('count',component.get('value')))
                self.assertTrue(descriptor['unit'])
                self.assertTrue(descriptor['stage'])
                self.assertTrue(all(r.get('evidence') for r in records))
                return records
            for kind in ('people','papers'):
                for component in summary['flows'][kind]['nodes']+summary['flows'][kind]['links']:
                    read(component)
            people_flow=summary['flows']['people']
            edge=next(e for e in people_flow['links'] if e['source']=='lookup-one' and e['target']=='matched')
            self.assertEqual({r['id'] for r in read(edge)},{'alias','b','c'})
            self.assertNotIn('a',{r['id'] for r in read(edge)})
            paper_flow=summary['flows']['papers']
            coauthors=read(next(n for n in paper_flow['nodes'] if n['id']=='coauthors'))
            self.assertEqual(coauthors[0]['evidence']['DBLP record'],'r1')
            self.assertIn('Representative link',coauthors[0]['evidence'])
            versions=read(next(n for n in paper_flow['nodes'] if n['id']=='versions'))
            self.assertEqual([r['id'] for r in versions],['r4'])
            self.assertEqual(versions[0]['evidence']['Version-linking rule'],'explicit_version')

    def test_source_ribbon_exposes_only_its_own_intake_rows_with_roles_and_affiliations(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            with closing(sqlite3.connect(data/'entity_resolution.sqlite')) as conn,conn:
                for field,type_ in [('source_db','TEXT'),('source_rowid','INTEGER'),('name_is_valid','INTEGER')]:
                    conn.execute('ALTER TABLE reviewer_observations ADD COLUMN '+field+' '+type_)
                conn.execute("UPDATE reviewer_observations SET source_db='sigmod_reviewers.sqlite',source_rowid=rowid,name_is_valid=1")
                conn.execute("INSERT INTO reviewer_observations VALUES ('a','https://example.test/vldb','vldb_reviewers.sqlite',1,1)")
            for filename,venue,count in [('sigmod_reviewers.sqlite','SIGMOD',7),('vldb_reviewers.sqlite','VLDB',1)]:
                with closing(sqlite3.connect(data/filename)) as conn,conn:
                    conn.execute('CREATE TABLE reviewer_records(id INTEGER PRIMARY KEY,year,venue,name,source_url,qualifies_db_seed,role,affiliation)')
                    conn.executemany('INSERT INTO reviewer_records VALUES (?,?,?,?,?,?,?,?)',
                        [(i,2024,venue,'Person '+str(i),'https://example.test/'+venue,1,'pc_member','Example University') for i in range(1,count+1)])
            summary=export_coverage(data,output,people,cohort,policy)
            edge=next(e for e in summary['flows']['people']['links'] if e['source']=='source-vldb_reviewers' and e['target']=='intake')
            descriptor=next(g for g in summary['flows']['groups'] if g['id']==edge['groupId'])
            records=json.loads((output/Path(descriptor['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))['records']
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['evidence']['Venue'],'VLDB')
            self.assertEqual(records[0]['evidence']['Role'],'pc_member')
            self.assertEqual(records[0]['evidence']['Affiliation'],'Example University')
            self.assertEqual(records[0]['evidence']['Decision'],'Ingested')

    def test_unverified_db_drilldown_keeps_cohort_work_units_separate_from_global_records(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':str(int(key!='r4')),'resolution_basis':'single_record'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2')]])
            write_rows(data/'reports/focus_publication_audit.csv',[
                {'pid':pid,'key':key,'work_id':work,'title':key,'counted':'False',
                 'reason':'Full-paper length cannot be verified','pages':pages}
                for pid,key,work,pages in [('a','r2','w2','1'),('a','r4','w2','2'),('c','r3','w3','3')]] + [
                    {'pid':'a','key':'unrelated','work_id':'w2','title':'unrelated','counted':'False',
                     'reason':'Not a research paper','pages':'999'}])
            summary=export_coverage(data,output,people,cohort,policy)
            uncertain=next(n for n in summary['flows']['papers']['nodes'] if n['id']=='uncertain')
            self.assertEqual(uncertain['count'],1)
            self.assertNotIn('review-length',uncertain['relatedGroupIds'])
            detail=next(g for g in summary['flows']['groups'] if g['id']=='flow-unverified-db-cohort')
            self.assertEqual((detail['count'],detail['unit'],detail['scope']),(1,'distinct works','Study cohort'))
            records=json.loads((output/Path(detail['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))['records']
            self.assertEqual([r['id'] for r in records],['w2'])
            self.assertEqual(records[0]['evidence']['DBLP evidence records'],'r2; r4')
            self.assertEqual(records[0]['evidence']['Recorded pages'],'1; 2')
            self.assertNotIn('unrelated',str(records[0]['evidence']))
            self.assertIn('r2 | pages: 1 | Full-paper length cannot be verified',records[0]['evidence']['Record-level DB evidence'])
            global_review=next(g for g in summary['quality'] if g['id']=='review-length')
            self.assertEqual((global_review['count'],global_review['unit']),(3,'DBLP records'))
            scenario=next(g for g in summary['quality'] if g['id']=='review-potential')
            self.assertIn('All PC members',scenario['label'])
            self.assertIn('what-if',scenario['label'])

    def test_pending_category_reviews_trace_versions_and_partition_context_without_counting_losses(self):
        from scripts.publication_category_reviews import REVIEW_FIELDS
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':str(int(key!='r4')),'resolution_basis':'single_record'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2')]])
            cohort[0]['ml']['papers'][1]['resolvedVersions']=[{'dblpKey':'r4'}]
            baseline=export_coverage(data,output,people,cohort,policy)
            reviews=[]
            for key,status in [('r2','confirmed'),('r4','pending'),('r3','pending')]:
                review=dict.fromkeys(REVIEW_FIELDS,'')
                review.update(dblp_key=key,title=key,year='2024',record_type='inproceedings',
                              category='workshop',status=status,evidence_url='https://example.test/'+key,
                              source_title='Exact track source',verification_method='live_primary_source',
                              reason='Exact workshop or conference track remains unclear',reviewed_on='2026-11-09',snapshot_id='test')
                reviews.append(review)
            write_rows(data/'publication_category_reviews.csv',reviews)
            summary=export_coverage(data,output,people,cohort,policy)
            self.assertEqual([(n['id'],n['count']) for n in baseline['flows']['papers']['nodes']],
                             [(n['id'],n['count']) for n in summary['flows']['papers']['nodes']])
            review=next(g for g in summary['quality'] if g['id']=='review-display-categories')
            self.assertEqual((review['count'],review['unit']),(2,'distinct works'))
            descriptors={g['id']:g for g in summary['flows']['groups']}
            for node_id,work,record_key in [('context','w2','r4'),('outside','w3','r3')]:
                node=next(n for n in summary['flows']['papers']['nodes'] if n['id']==node_id)
                group_id='flow-pending-categories-'+node_id
                self.assertIn(group_id,node['relatedGroupIds'])
                descriptor=descriptors[group_id]
                records=json.loads((output/Path(descriptor['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))['records']
                self.assertEqual([r['id'] for r in records],[work])
                self.assertEqual(records[0]['evidence']['Reviewed DBLP records'],record_key)
                self.assertEqual(records[0]['evidence']['Reviewed on'],'2026-11-09')
                self.assertEqual(records[0]['reviewSources'],['https://example.test/'+record_key])
                self.assertEqual(records[0]['reviewSourceLabel'],'Publication category sources')
                self.assertNotIn('sources',records[0])
            uncertain=next(n for n in summary['flows']['papers']['nodes'] if n['id']=='uncertain')
            self.assertFalse(any('pending-categories' in key for key in uncertain['relatedGroupIds']))

    def fixture(self, directory):
        data, output = Path(directory)/'data', Path(directory)/'website'
        data.mkdir(); output.mkdir(); (data/'reports').mkdir()
        conn = sqlite3.connect(data/'entity_resolution.sqlite')
        conn.executescript('''
            CREATE TABLE name_resolution (name_key TEXT, representative_name TEXT, resolved_dblp_pid TEXT, status TEXT, all_candidate_count INTEGER);
            CREATE TABLE name_groups (name_key TEXT, affiliations_json TEXT, venues_json TEXT);
            CREATE TABLE reviewer_observations (name_key TEXT, source_url TEXT);
            CREATE TABLE dblp_candidates (name_key TEXT, dblp_pid TEXT, affiliation_similarity REAL);
            CREATE TABLE dblp_people (dblp_pid TEXT, canonical_name TEXT);
        ''')
        names = [('a','Alice','a','resolved_unique_exact',2), ('alias','A.','a','resolved_unique_exact',1),
                 ('b','Bob','b','resolved_unique_exact',1), ('c','C','c','resolved_unique_exact',1),
                 ('d','D',None,'ambiguous_exact_name',2), ('e','E',None,'candidate_unique_variant',1),
                 ('f','F',None,'unresolved_no_local_dblp_candidate',0)]
        conn.executemany('INSERT INTO name_resolution VALUES (?,?,?,?,?)', names)
        conn.executemany('INSERT INTO name_groups VALUES (?,?,?)', [(r[0],'[]','[]') for r in names])
        conn.executemany('INSERT INTO reviewer_observations VALUES (?,?)', [(r[0],'https://example.test/service') for r in names])
        conn.executemany('INSERT INTO dblp_people VALUES (?,?)', [('a','Alice'),('b','Bob'),('c','C')])
        conn.executemany('INSERT INTO dblp_candidates VALUES (?,?,?)', [('a','a',0),('a','b',0.5)])
        conn.commit(); conn.close()
        def paper(key, work, category, scope='side'):
            return dict(dblpKey=key, workId=work, publicationClass=category, scope=scope, title=key,
                        year=2024, venueLabel='Test', authorPosition=None, authorCount=2)
        def person(pid, label, papers, included=False):
            return dict(dblpPid=pid,id='dblp:'+pid,name=pid,primaryAffiliation='',
                        db={'events':[{'sourceUrl':'https://example.test/service'}]},
                        focus={'label':label,'included':included,'dbPapers':2,'mlPapers':0,'reason':'Test reason'},ml={'papers':papers})
        a=person('a','DB-focused',[paper('r1','w1','main_ml','main'),paper('r2','w2','workshop')],True)
        b=person('b','Limited evidence',[paper('r2','w2','other_conference')])
        c=person('c','DB-focused',[paper('r3','w3','other')])
        raw=[{'dblp_pid':'a','dblp_key':key,'title':key,'year':'2024','url':''} for key in ['r1','r2','r3','r4']]
        raw.append(dict(raw[0]))  # another author link is not another record
        write_rows(data/'dblp_publications_for_db_people.csv',raw)
        write_rows(data/'publication_entity_resolution.csv',[
            {'dblp_key':key,'is_canonical':'1' if key!='r4' else '0','resolution_basis':'single_record' if key!='r4' else 'explicit_version'} for key in ['r1','r2','r3','r4']])
        write_rows(data/'reports/focus_publication_audit.csv',[
            {'pid':'a','key':'r2','title':'r2','counted':'False','reason':'Full-paper length cannot be verified'},
            {'pid':'b','key':'r2','title':'r2','counted':'False','reason':'Full-paper length cannot be verified'}])
        policy={'candidates':[a,b]}
        return data,output,[a,b,c],[a],policy

    def test_every_stack_reconciles_and_units_do_not_double_count_aliases_or_coauthors(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            summary=export_coverage(data,output,people,cohort,policy)
            for chart in summary['people']+summary['papers']:
                self.assertEqual(sum(g['count'] for g in chart['groups']),chart['total'])
            self.assertEqual(summary['people'][0]['total'],7)
            self.assertEqual([g['count'] for g in summary['people'][0]['groups']],[4,1,1,1])
            self.assertEqual(summary['people'][1]['total'],3)
            self.assertEqual([g['count'] for g in summary['people'][1]['groups']],[1,1,0,0,1])
            self.assertEqual(summary['collection']['personPaperLinks'],5)
            self.assertEqual(summary['collection']['records'],4)
            self.assertEqual(summary['papers'][0]['total'],4)
            self.assertEqual([g['count'] for g in summary['papers'][0]['groups']],[3,1,0])
            self.assertEqual([g['count'] for g in summary['papers'][1]['groups']],[1,1,1])

    def test_ambiguous_candidate_evidence_is_deduplicated_and_does_not_change_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            with closing(sqlite3.connect(data/'entity_resolution.sqlite')) as conn, conn:
                conn.execute('ALTER TABLE name_resolution ADD COLUMN notes TEXT')
                conn.execute('ALTER TABLE dblp_candidates ADD COLUMN match_type TEXT')
                conn.execute('ALTER TABLE dblp_candidates ADD COLUMN matched_alias TEXT')
                for field in ['affiliations_json','verified_orcids_json','snapshot_id']:
                    conn.execute('ALTER TABLE dblp_people ADD COLUMN '+field+' TEXT')
                conn.execute("UPDATE name_resolution SET notes='Two exact profiles remain' WHERE name_key='d'")
                conn.execute("UPDATE name_groups SET affiliations_json=?,venues_json=? WHERE name_key='d'",(json.dumps(['Concordia University']),json.dumps(['SIGMOD'])))
                conn.execute("UPDATE dblp_people SET affiliations_json=?,verified_orcids_json=?,snapshot_id='test-snapshot' WHERE dblp_pid='a'",(json.dumps(['Concordia University, Canada']),json.dumps(['0000-0001-2345-6789'])))
                conn.execute("INSERT INTO dblp_people VALUES ('x','Different Alice','[]','[]','test-snapshot')")
                conn.executemany('INSERT INTO dblp_candidates VALUES (?,?,?,?,?)',[
                    ('d','a',0.4,'exact_alias','Alice'),('d','a',0.8,'first_last_variant','Alice A.'),
                    ('d','x',0,'exact_alias','Alice 0002'),('e','x',0,'initial_last_variant','A. Alice')])
                before=conn.execute('SELECT * FROM name_resolution ORDER BY name_key').fetchall()
            s=export_coverage(data,output,people,cohort,policy)
            ambiguous=s['people'][0]['groups'][1]
            payload=json.loads((output/Path(ambiguous['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))
            review=payload['records'][0]['identityReview']
            self.assertEqual(review['serviceAffiliations'],['Concordia University'])
            self.assertEqual(review['serviceVenues'],['SIGMOD'])
            self.assertEqual(review['recordedNote'],'Two exact profiles remain')
            self.assertEqual(review['exactCandidateCount'],2)
            self.assertEqual(len(review['candidates']),2)
            a,x=review['candidates']
            self.assertEqual(a['pid'],'a')
            self.assertEqual(a['affiliationOverlap'],0.8)
            self.assertTrue(a['alreadyResolved'])
            self.assertEqual(a['matchTypes'],['exact_alias','first_last_variant'])
            self.assertEqual(a['orcids'],['0000-0001-2345-6789'])
            self.assertEqual(a['snapshot'],'test-snapshot')
            self.assertEqual(x['affiliations'],[])
            self.assertIsNone(x['affiliationOverlap'])  # missing evidence is not negative evidence
            self.assertFalse(x['alreadyResolved'])
            self.assertEqual(x['url'],'https://dblp.org/pid/x')
            with closing(sqlite3.connect(data/'entity_resolution.sqlite')) as conn:
                self.assertEqual(conn.execute('SELECT * FROM name_resolution ORDER BY name_key').fetchall(),before)

    def test_candidate_details_are_explicit_when_old_metadata_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            s=export_coverage(data,output,people,cohort,policy)
            group=s['people'][0]['groups'][1]
            payload=json.loads((output/Path(group['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))
            review=payload['records'][0]['identityReview']
            self.assertEqual(review['candidates'],[])
            self.assertEqual(review['recordedCandidateCount'],2)

    def test_sparse_accepted_matches_keep_the_selected_identity_and_review_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            with closing(sqlite3.connect(data/'entity_resolution.sqlite')) as conn, conn:
                conn.execute('ALTER TABLE name_groups ADD COLUMN observation_count INTEGER DEFAULT 1')
                conn.execute('ALTER TABLE dblp_people ADD COLUMN affiliations_json TEXT DEFAULT \'[]\'')
                conn.execute('ALTER TABLE dblp_people ADD COLUMN verified_orcids_json TEXT DEFAULT \'[]\'')
                conn.execute('ALTER TABLE dblp_candidates ADD COLUMN match_type TEXT')
                conn.execute("UPDATE dblp_candidates SET match_type=CASE WHEN dblp_pid='a' THEN 'exact_alias' ELSE 'first_last_variant' END")
            summary=export_coverage(data,output,people,cohort,policy)
            review=next(g for g in summary['quality'] if g['id']=='review-sparse-identities')
            self.assertEqual(review['count'],1)
            self.assertTrue(review['reviewOnly'])
            self.assertEqual((review['scope'],review['unit']),('All matched researchers','name groups'))
            payload=json.loads((output/Path(review['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))
            evidence=payload['records'][0]['identityReview']
            self.assertEqual(evidence['currentPid'],'a')
            self.assertEqual(evidence['candidates'][0]['pid'],'a')
            self.assertTrue(evidence['candidates'][0]['selected'])
            self.assertFalse(evidence['candidates'][1]['selected'])
            self.assertIn('not proof of an error',evidence['reason'])
            matched=next(n for n in summary['flows']['people']['nodes'] if n['id']=='matched')
            self.assertIn(review['id'],matched['relatedGroupIds'])
            potential=next(g for g in summary['quality'] if g['id']=='review-potential')
            self.assertTrue(potential['scenario'])
            self.assertEqual(potential['unit'],'people')

    def test_conflicts_and_quality_flags_are_explicit_and_lazy_lists_match_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            summary=export_coverage(data,output,people,cohort,policy)
            conflict=next(g for g in summary['classifications']['all']['groups'] if g['label']=='Conflicting categories')
            self.assertEqual(conflict['count'],1)
            quality={g['id']:g for g in summary['quality']}
            self.assertEqual(quality['review-length']['affectedPeople'],2)
            self.assertEqual(quality['review-length']['includedPeople'],1)
            self.assertEqual(quality['review-length']['excludedPeople'],1)
            self.assertEqual(quality['review-length']['potentialAdditions'],0)
            self.assertFalse(quality['review-input']['measured'])
            self.assertEqual(quality['review-order']['count'],1)
            self.assertEqual(quality['review-affiliation']['count'],1)
            self.assertEqual(quality['review-source']['count'],0)
            for chart in summary['assignmentEvidence'].values():
                self.assertEqual(sum(g['count'] for g in chart['groups']),chart['total'])
            def check(value):
                if isinstance(value,dict):
                    if 'detailUrl' in value:
                        target=output/Path(value['detailUrl']).relative_to('data')
                        self.assertEqual(len(json.loads(target.read_text())['records']),value['count'])
                    for child in value.values():check(child)
                elif isinstance(value,list):
                    for child in value:check(child)
            check(summary)

    def test_unavailable_collection_is_reported_and_only_older_generated_lists_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            first=export_coverage(data,output,people,cohort,policy)
            identity=next(g for g in first['quality'] if g['id']=='review-identities')
            old=output/Path(identity['detailUrl']).relative_to('data')
            people[0]['name']='Changed name'
            (data/'dblp_publications_for_db_people.csv').unlink()
            second=export_coverage(data,output,people,cohort,policy)
            self.assertFalse(second['collection']['available'])
            self.assertEqual(len(second['papers']),1)
            self.assertTrue(old.exists())  # preceding summary may still be open
            people[0]['name']='Changed name again'
            export_coverage(data,output,people,cohort,policy)
            self.assertFalse(old.exists())

    def test_pre_filter_audit_finds_records_absent_from_loaded_bibliography(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'input.csv'
            rows=[{'dblp_pid':'a','year':'2024','title':'Valid','dblp_key':'valid'},
                  {'dblp_pid':'a','year':'','title':'Missing year','dblp_key':'no-year'},
                  {'dblp_pid':'b','year':'invalid','title':'Missing year','dblp_key':'no-year'},
                  {'dblp_pid':'a','year':'2024','title':'','dblp_key':'no-title'},
                  {'dblp_pid':'a','year':'2024','title':'Missing key','dblp_key':''},
                  {'dblp_pid':'unknown','year':'','title':'Not matched','dblp_key':'ignored'}]
            write_rows(path,rows)
            people=[{'dblpPid':'a','name':'Alice'},{'dblpPid':'b','name':'Bob'}]
            issues,affected=audit_publication_input(path,people)
            self.assertEqual(affected,{'a','b'})
            self.assertEqual(len(issues),3)  # coauthors share one broken record
            self.assertIn('Alice, Bob',next(r for r in issues if r['id']=='no-year')['detail'])
            loaded,_,_=load_publications(path,{'a','b'})
            self.assertEqual(set(loaded['a']['papers']),{'valid'})
            self.assertNotIn('b',loaded)

    def test_rebuild_reuses_valid_lists_and_repairs_a_damaged_list(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            first=export_coverage(data,output,people,cohort,policy)
            group=first['people'][0]['groups'][0]
            path=output/Path(group['detailUrl']).relative_to('data')
            before=path.stat().st_mtime_ns
            export_coverage(data,output,people,cohort,policy)
            self.assertEqual(path.stat().st_mtime_ns,before)
            path.write_text('{}',encoding='utf-8')
            export_coverage(data,output,people,cohort,policy)
            self.assertEqual(len(json.loads(path.read_text())['records']),group['count'])

    def test_changed_summary_retains_previous_lists_and_prunes_only_older_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            def publish(title):
                people[0]['ml']['papers'][0]['title']=title
                summary=export_coverage(data,output,people,cohort,policy)
                group=next(g for chart in summary['papers'] for g in chart['groups'] if g['id']=='works-main')
                return output/Path(group['detailUrl']).relative_to('data')
            first=publish('First title')
            original=first.read_bytes()
            second=publish('Corrected title')
            self.assertNotEqual(first,second)
            self.assertTrue(first.exists(), 'An open tab with the preceding summary still needs its immutable list')
            self.assertEqual(first.read_bytes(),original)
            self.assertTrue(second.exists())
            publish('Corrected title')
            self.assertTrue(first.exists(), 'An identical rebuild does not advance the retained generation')
            with mock.patch.dict('scripts.data_coverage.LABELS', {'workshop':'Renamed workshop label'}):
                publish('Corrected title')
            self.assertTrue(first.exists(), 'Changed summary wording with the same assets does not advance retention')
            third=publish('Final title')
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())
            self.assertFalse(first.exists(), 'Only the current and preceding changed generation are retained')

    def test_failed_summary_publication_preserves_previous_manifest_and_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            summary=export_coverage(data,output,people,cohort,policy)
            target=output/'coverage_summary.json'
            previous=target.read_bytes()
            group=next(g for chart in summary['papers'] for g in chart['groups'] if g['id']=='works-main')
            old_list=output/Path(group['detailUrl']).relative_to('data')
            people[0]['ml']['papers'][0]['title']='Changed after initial publication'
            original_replace=Path.replace
            def fail_summary_replace(path,destination):
                if destination==target:
                    raise OSError('fixture: interrupted summary publication')
                return original_replace(path,destination)
            with mock.patch.object(Path,'replace',fail_summary_replace):
                with self.assertRaisesRegex(OSError,'interrupted summary'):
                    export_coverage(data,output,people,cohort,policy)
            self.assertEqual(target.read_bytes(),previous)
            self.assertTrue(old_list.exists())

    def test_damaged_previous_summary_does_not_authorize_list_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            summary=export_coverage(data,output,people,cohort,policy)
            group=next(g for chart in summary['papers'] for g in chart['groups'] if g['id']=='works-main')
            old_list=output/Path(group['detailUrl']).relative_to('data')
            (output/'coverage_summary.json').write_text('{broken',encoding='utf-8')
            people[0]['ml']['papers'][0]['title']='New title'
            export_coverage(data,output,people,cohort,policy)
            self.assertTrue(old_list.exists())
            json.loads((output/'coverage_summary.json').read_text(encoding='utf-8'))

    def test_incomplete_source_record_without_resolution_remains_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            raw=data/'dblp_publications_for_db_people.csv'
            with raw.open(encoding='utf-8',newline='') as handle:
                rows=list(csv.DictReader(handle))
            rows.append({'dblp_pid':'b','dblp_key':'','title':'Lost upstream','year':'','url':''})
            write_rows(raw,rows)
            summary=export_coverage(data,output,people,cohort,policy)
            unresolved=next(g for g in summary['papers'][0]['groups'] if g['id']=='papers-unresolved')
            self.assertEqual(unresolved['count'],1)
            source=next(g for g in summary['quality'] if g['id']=='review-source')
            self.assertEqual(source['count'],1)
            self.assertEqual(source['excludedPeople'],1)

    def test_potential_inclusions_use_existing_policy_and_deduplicate_versions(self):
        def paper(key,year,work=None):
            return {'dblpKey':key,'workId':work or key,'year':year,'scope':'side'}
        def person(pid,papers,years=(2022,)):
            return {'dblpPid':pid,'name':pid,'id':pid,'focus':{'included':False,'label':'Limited evidence'},
                    'ml':{'papers':papers},'db':{'events':[{'venue':'SIGMOD','role':'pc_member','year':y} for y in years]}}
        a=person('a',[paper('a1',2020),paper('a2',2021),paper('a3',2022),paper('a4',2022),paper('a5',2023)])
        b=person('b',[dict(paper('b1',2023,'b-work'),resolvedVersions=[paper('b2',2024)])],(2022,2023))
        c=person('c',[paper('c1',2022),paper('c2',2023)],(2022,2023))
        d=person('d',[paper('d1',2022),paper('d2',2023)],(2022,))
        outside=person('outside',[paper('o1',2022),paper('o2',2023)],(2022,2023))
        people=[a,b,c,d,outside]
        rows=[]
        for p in people:
            for work in p['ml']['papers']:
                for v in [work,*work.get('resolvedVersions',[])]:
                    accepted=v['dblpKey'] in {'a1','a2','a3','a4'}
                    rows.append({'pid':p['dblpPid'],'key':v['dblpKey'],'title':v['dblpKey'],
                                 'counted':str(accepted),'reason':'Counted' if accepted else 'Full-paper length cannot be verified'})
        records,affected,potential=length_review_impact(rows+rows,people,{p['dblpPid']:p for p in people if p is not outside})
        self.assertEqual(affected,{'a','b','c','d','outside'})
        self.assertEqual({p['id'] for p in potential},{'a','c'})
        self.assertEqual(len(records),9)
        self.assertTrue(all(not p['focus']['included'] for p in people))

    def test_sankeys_trace_source_roles_merges_and_study_exclusions_with_conservation(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            with closing(sqlite3.connect(data/'entity_resolution.sqlite')) as conn, conn:
                for field,type_ in [('source_db','TEXT'),('source_rowid','INTEGER'),('name_is_valid','INTEGER')]:
                    conn.execute('ALTER TABLE reviewer_observations ADD COLUMN '+field+' '+type_)
                conn.execute("UPDATE reviewer_observations SET source_db='sigmod_reviewers.sqlite',source_rowid=rowid,name_is_valid=1")
                conn.executemany('INSERT INTO reviewer_observations VALUES (?,?,?,?,?)',
                    [('a','https://example.test/service','sigmod_reviewers.sqlite',8,1),('a','https://example.test/service','sigmod_reviewers.sqlite',9,1)])
            with closing(sqlite3.connect(data/'sigmod_reviewers.sqlite')) as conn, conn:
                conn.executescript('CREATE TABLE reviewer_records(id INTEGER PRIMARY KEY,year,venue,name,source_url,qualifies_db_seed); CREATE TABLE source_pages(year,url);')
                conn.executemany('INSERT INTO reviewer_records VALUES (?,?,?,?,?,?)',
                    [(i,2024,'SIGMOD','Name '+str(i),'https://example.test/service',int(i!=10)) for i in range(1,11)])
                conn.executemany('INSERT INTO source_pages VALUES (?,?)',[(2024,'https://example.test/service'),(2024,'https://example.test/empty')])
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':str(int(key!='r4')),'resolution_basis':'single_record'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2')]])
            raw=data/'dblp_publications_for_db_people.csv'
            cache={'snapshot_id':'test','output_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),
                   'stats':{'accepted':5,'records':1000,'skipped_alias_ambiguity':2}}
            raw.with_suffix('.csv.scan.json').write_text(json.dumps(cache),encoding='utf-8')
            s=export_coverage(data,output,people,cohort,policy)
            p={n['id']:n for n in s['flows']['people']['nodes']}
            self.assertEqual(p['source-sigmod_reviewers']['count'],10)
            self.assertEqual(p['intake-Outside seed roles']['count'],1)
            self.assertEqual(p['repeat-service']['count'],2)
            self.assertEqual(p['aliases']['count'],1)
            self.assertEqual(p['pc']['count'],2)
            self.assertEqual(p['outside-pc']['count'],1)
            self.assertEqual(p['focus-0']['count'],1)
            self.assertEqual(s['flows']['people']['sources'][0]['pages'],2)
            p={n['id']:n for n in s['flows']['papers']['nodes']}
            self.assertEqual(p['candidates']['count'],7)
            self.assertEqual(p['ambiguous-authors']['count'],2)
            self.assertEqual(p['coauthors']['count'],1)
            self.assertEqual(p['versions']['count'],1)
            self.assertEqual(p['main']['count'],1)
            self.assertTrue(s['flows']['papers']['snapshot']['verified'])
            for flow in (s['flows']['people'],s['flows']['papers']):
                for node in flow['nodes']:
                    incoming=sum(l['value'] for l in flow['links'] if l['target']==node['id'])
                    outgoing=sum(l['value'] for l in flow['links'] if l['source']==node['id'])
                    if incoming:self.assertEqual(incoming,node['count'],node['id'])
                    if outgoing:self.assertEqual(outgoing,node['count'],node['id'])
                self.assertTrue(any(l['kind']=='merged' for l in flow['links']))

    def test_sankey_reports_missing_trace_and_stale_scan_log_instead_of_inventing_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            s=export_coverage(data,output,people,cohort,policy)
            self.assertIn('Source-to-intake',s['flows']['people']['notes'][0])
            self.assertIn('lacks work IDs',s['flows']['papers']['unavailable'])
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':'1','resolution_basis':'single_record'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2')]])
            (data/'dblp_publications_for_db_people.csv.scan.json').write_text(json.dumps({'output_sha256':'stale','stats':{'accepted':5,'records':999}}))
            s=export_coverage(data,output,people,cohort,policy)
            self.assertFalse(s['flows']['papers']['snapshot']['verified'])
            self.assertIsNone(s['flows']['papers']['checks']['authorAmbiguity'])
            self.assertIsNone(s['flows']['papers']['snapshot']['xmlRecordsScanned'])

    def test_candidate_lookup_counts_name_groups_and_keeps_disambiguation_only_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            with closing(sqlite3.connect(data/'entity_resolution.sqlite')) as conn, conn:
                conn.execute("INSERT INTO name_resolution VALUES ('no-person','Only disambiguation',NULL,'ambiguous_exact_name',0)")
                conn.execute("INSERT INTO name_groups VALUES ('no-person','[]','[]')")
                conn.execute("INSERT INTO reviewer_observations VALUES ('no-person','https://example.test/source')")
            s=export_coverage(data,output,people,cohort,policy)
            f=s['flows']['people']; nodes={n['id']:n for n in f['nodes']}
            self.assertEqual(f['stages'][3],'Candidate lookup')
            self.assertEqual(nodes['lookup-none']['count'],2)
            self.assertEqual(nodes['lookup-one']['count'],4)
            self.assertEqual(nodes['lookup-several']['count'],2)
            links={(e['source'],e['target']):e['value'] for e in f['links']}
            self.assertEqual(links['lookup-none','ambiguous'],1)
            self.assertEqual(links['lookup-none','unmatched'],1)
            self.assertEqual(nodes['matched']['stage'],4)
            self.assertEqual(nodes['focus-0']['stage'],7)

    def test_useful_review_details_are_attached_to_sankey_drilldowns(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            people[1]['focus'].update(dbPublicationYears=1,serviceYears=1)
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':'1','resolution_basis':'single_record'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2')]])
            s=export_coverage(data,output,people,cohort,policy)
            limited=next(n for n in s['flows']['people']['nodes'] if n['id']=='focus-1')
            self.assertIn('flow-limited-db-evidence',limited['relatedGroupIds'])
            self.assertIn('1: Too little DB publication evidence',limited['details'])
            context=next(n for n in s['flows']['papers']['nodes'] if n['id']=='context')
            self.assertIn('flow-context-workshop',context['relatedGroupIds'])
            for node in [limited,context]:
                for key in node['relatedGroupIds']:
                    g=next(g for g in s['flows']['groups'] if g['id']==key)
                    records=json.loads((output/Path(g['detailUrl']).relative_to('data')).read_text(encoding='utf-8'))['records']
                    self.assertEqual(len(records),g['count'])

    def test_limited_evidence_explanations_are_exclusive(self):
        from scripts.coverage_flow import limited_evidence_reason
        for db,ml,years,service,expected in [(0,0,0,1,'no-papers'),(1,2,1,2,'ratio'),
                (2,0,1,2,'db-evidence'),(2,0,2,1,'pc-service')]:
            self.assertEqual(limited_evidence_reason(dict(label='Limited evidence',dbPapers=db,mlPapers=ml,
                                                         dbPublicationYears=years,serviceYears=service)),expected)

    def test_sankey_keeps_invalid_unresolved_and_untraced_work_counts_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            data,output,people,cohort,policy=self.fixture(directory)
            raw=data/'dblp_publications_for_db_people.csv'
            with raw.open(encoding='utf-8') as handle:raw_rows=list(csv.DictReader(handle))
            raw_rows += [{'dblp_pid':'a','dblp_key':key,'title':key,'year':'','url':''} for key in ['r5','r6']]
            write_rows(raw,raw_rows)
            write_rows(data/'publication_entity_resolution.csv',[
                {'dblp_key':key,'work_id':work,'is_canonical':'1','resolution_basis':'single_record'}
                for key,work in [('r1','w1'),('r2','w2'),('r3','w3'),('r4','w2'),('r5','w5')]])
            write_rows(data/'dblp_publication_entities_for_db_people.csv',[dict(raw_rows[-2],work_id='w5')])
            s=export_coverage(data,output,people,cohort,policy)
            nodes={n['id']:n for n in s['flows']['papers']['nodes']}
            self.assertEqual(nodes['undecided']['count'],1)
            self.assertEqual(nodes['invalid-work']['count'],1)
            self.assertEqual(nodes['website']['count'],3)
            # A valid copy absent for another reason must not be called invalid metadata.
            write_rows(data/'dblp_publication_entities_for_db_people.csv',[
                dict(raw_rows[-2],work_id='w5'),dict(raw_rows[-2],work_id='w5',year='2024',dblp_pid='unknown')])
            s=export_coverage(data,output,people,cohort,policy)
            nodes={n['id']:n for n in s['flows']['papers']['nodes']}
            self.assertNotIn('invalid-work',nodes)
            self.assertEqual(nodes['missing-work']['count'],1)


if __name__=='__main__':unittest.main()
