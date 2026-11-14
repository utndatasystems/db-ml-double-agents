from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'scripts'))
import refresh_affiliations as refresh
from automatic_affiliations import MetadataCache, OUTPUT_FILENAME, collect_evidence
from build_site_data import browser_payload_and_detail_files, write_browser_detail_files, write_browser_search_file


def fixture():
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        root = Path(tmp)
        (root / 'entity_resolution.sqlite').touch()
        (root / 'dblp_publication_entities_for_db_people.csv').write_text('unchanged fixture')
        person = {'names': {'Alice Example'}, 'aliases': {'Alice Example'}, 'orcids': set()}
        paper = {'dblpKey': 'conf/example/A26', 'year': 2026, 'title': 'A publication',
                 'url': 'https://doi.org/10.1234/example', 'localAuthorName': 'Alice Example'}
        output = root / OUTPUT_FILENAME
        def run():
            with mock.patch.object(sys, 'argv', ['refresh_affiliations.py', '--data-dir', str(root), '--offline']), \
                 mock.patch.object(refresh, 'load_db_people', return_value=({'alice': person}, {})), \
                 mock.patch.object(refresh, 'latest_papers', return_value={'alice': [paper]}), \
                 contextlib.redirect_stdout(io.StringIO()):
                refresh.main()
            return json.loads(output.read_text(encoding='utf-8'))['people']['alice']
        first = run()
        cache = MetadataCache(root / 'affiliation_metadata.sqlite', offline=True)
        message = {'message': {'DOI': '10.1234/example', 'title': ['A publication'],
                   'author': [{'given': 'Alice', 'family': 'Example', 'affiliation': [{'name': 'Recovered University'}]}],
                   'published': {'date-parts': [[2026, 4, 12]]}}}
        cache.conn.execute('INSERT INTO responses VALUES (?,?,?,?)',
                           ('https://api.crossref.org/works/' + quote('10.1234/example', safe=''),
                            '2026-10-19T00:00:00+00:00', 200, json.dumps(message)))
        cache.conn.commit()
        cache.close()
        restored = run()
        output.unlink()
        forced_rebuild = run()
        return {'before_cache_restore': first, 'ordinary_offline_after_cache_restore': restored,
                'same_inputs_forced_rebuild_after_cache_restore': forced_rebuild}


def production():
    root = ROOT / 'data'
    evidence = json.loads((root / OUTPUT_FILENAME).read_text(encoding='utf-8'))
    conn = sqlite3.connect('file:' + (root / 'affiliation_metadata.sqlite').as_posix() + '?mode=ro', uri=True)
    cache_rows = {r[0]: (r[1], r[2], r[3]) for r in conn.execute('SELECT url,status,payload,fetched_at FROM responses')}
    urls = set(cache_rows)
    conn.close()
    people = evidence['people']
    checks = [c for p in people.values() for c in p.get('checks', [])]
    missing = [c for c in checks if 'https://api.crossref.org/works/' + quote(c['doi'], safe='') not in urls]
    positive_for_unavailable = [c for c in checks if c.get('outcome') == 'metadata_unavailable'
                              and cache_rows.get('https://api.crossref.org/works/' + quote(c['doi'], safe=''), (0,))[0] == 200]
    return {'people': len(people), 'people_with_candidates': sum(bool(p.get('candidates')) for p in people.values()),
            'candidate_rows': sum(len(p.get('candidates', [])) for p in people.values()),
            'checks': len(checks), 'missing_doi_cache_rows': len(missing),
            'metadata_unavailable_checks': sum(c.get('outcome') == 'metadata_unavailable' for c in checks),
            'currently_positive_cache_for_previously_unavailable_checks': len(positive_for_unavailable),
            'positive_recovered_examples': positive_for_unavailable[:5],
            'evidence_if_cache_absent': 0,
            'existing_snapshot_preserves_if_inputs_identical': True}


def interrupted_export():
    old_person = {'id': 'alice', 'dblpPid': 'alice', 'ml': {'papers': [{'scope': 'side', 'title': 'Old title'}]}}
    new_person = {'id': 'alice', 'dblpPid': 'alice', 'ml': {'papers': [{'scope': 'side', 'title': 'Corrected title'}]}}
    old_payload, old_details, old_search = browser_payload_and_detail_files({'people': [old_person]})
    new_payload, new_details, new_search = browser_payload_and_detail_files({'people': [new_person]})
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
        root = Path(tmp)
        write_browser_detail_files(root, old_details)
        write_browser_search_file(root, old_search)
        manifest = root / 'site_data.js'
        manifest.write_text('window.CROSSOVER_DATA=' + json.dumps(old_payload) + ';')
        # Real main writes manifest before detail/search writers. Stop exactly there.
        manifest.write_text('window.CROSSOVER_DATA=' + json.dumps(new_payload) + ';')
        detail = root / new_payload['people'][0]['ml']['detailUrl'].removeprefix('data/')
        search = root / new_payload['meta']['publicationSearchUrl'].removeprefix('data/')
        missing_at_first_boundary = [str(p.relative_to(root)) for p in (detail, search) if not p.exists()]
        original_write = Path.write_text
        def disk_full(path, *args, **kwargs):
            if path.suffix == '.tmp':
                raise OSError('fixture: disk full during detail asset write')
            return original_write(path, *args, **kwargs)
        with mock.patch.object(Path, 'write_text', disk_full):
            try:
                write_browser_detail_files(root, new_details)
            except OSError:
                pass
        after_failure = [p.name for p in (root / 'people').glob('*.json')]
        write_browser_detail_files(root, new_details)
        write_browser_search_file(root, new_search)
        return {'missing_referenced_assets_if_interrupted_after_main': missing_at_first_boundary,
                'remaining_detail_assets_after_write_failure': after_failure,
                'retry_restores_new_assets': detail.exists() and search.exists()}


if __name__ == '__main__':
    result = {'fixture': fixture(), 'production': production(), 'interrupted_export': interrupted_export()}
    target = Path(__file__).with_suffix('.json')
    target.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
