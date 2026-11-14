"""Check a self-contained static website before it is published. No source refresh."""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

try:
    from .site_paths import WEBSITE_DIR
    from .home_population import home_population_summary
    from .build_matrix_stats import build_stats
except ImportError:
    from site_paths import WEBSITE_DIR
    from home_population import home_population_summary
    from build_matrix_stats import build_stats


class PageReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if value and key in {'src', 'href', 'data-methodology-data-src', 'data-methodology-app-src'}:
                self.urls.append(value)


def local_file(root, url):
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = (root / unquote(parsed.path)).resolve()
    if parsed.path.startswith('/') or not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f'Missing or unsafe website reference: {url}')
    return path


def wrapper(path, prefix):
    text = path.read_text(encoding='utf-8').strip()
    if not text.startswith(prefix):
        raise ValueError(f'Invalid browser data wrapper: {path.name}')
    return json.loads(text[len(prefix):].removesuffix(';'))


def data_references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith('Url') and isinstance(child, str) and child.startswith('data/'):
                yield child
            yield from data_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from data_references(child)


def validate_static_site(directory=WEBSITE_DIR):
    root = Path(directory).resolve()
    entries = list(root.rglob('*'))
    for path in entries:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'Linked file outside website: {path}')
    files = [p for p in entries if p.is_file()]
    for path in files:
        if path.suffix.lower() in {'.md', '.sqlite', '.tmp'} or path.name in {'site_data.json', 'matrix_stats.json'}:
            raise ValueError(f'Private build/documentation file in published site: {path.name}')
    for name in ('index.html', 'people.html', 'all.html'):
        page = local_file(root, name)
        parser = PageReferences()
        parser.feed(page.read_text(encoding='utf-8'))
        for url in parser.urls:
            local_file(root, url)

    site = wrapper(local_file(root, 'data/site_data.js'), 'window.CROSSOVER_DATA=')
    matrix = wrapper(local_file(root, 'data/matrix_stats.js'), 'window.CROSSOVER_MATRIX_STATS=')
    home = json.loads(local_file(root, 'data/home_summary.json').read_text(encoding='utf-8'))
    coverage = json.loads(local_file(root, 'data/coverage_summary.json').read_text(encoding='utf-8'))
    if not site.get('meta', {}).get('dataReady') or not isinstance(site.get('people'), list):
        raise ValueError('Researcher data is not ready')

    pending = set(data_references(site)) | set(data_references(coverage))
    checked = set()
    while pending:
        url = pending.pop()
        if url in checked:
            continue
        path = local_file(root, url)
        body = path.read_bytes()
        digest = re.search(r'[-.]([0-9a-f]{12})\.json$', path.name)
        if digest:
            # Builders hash compact JSON before its final newline, or coverage
            # bytes including it. Git/Windows may convert LF to CRLF.
            normalized = body.replace(b'\r\n', b'\n')
            hashes = {hashlib.sha256(value).hexdigest()[:12]
                      for value in (normalized, normalized.removesuffix(b'\n'))}
            if digest[1] not in hashes:
                raise ValueError(f'Content hash mismatch: {url}')
        pending.update(set(data_references(json.loads(body))) - checked)
        checked.add(url)

    events = site.get('browserServiceEvents', [])
    for person in site['people']:
        db = person.get('db', {})
        if 'eventRefs' in db:
            if any(type(index) is not int or not 0 <= index < len(events) for index in db['eventRefs']):
                raise ValueError('Invalid shared service record reference')
            db['events'] = [events[index] for index in db['eventRefs']]
    if home != home_population_summary(site['people'], site.get('mainMlVenueYearTotals', {})):
        raise ValueError('Home summary does not match the researcher data')
    service = [(event['venue'], person['dblpPid'], event['year'])
               for person in site['people'] for event in person.get('db', {}).get('events', [])
               if event.get('venue') and isinstance(event.get('year'), int)]
    expected_matrix = build_stats(site, service)
    for value in (matrix, expected_matrix):
        value.get('meta', {}).pop('generatedAt', None)
    if matrix != expected_matrix:
        raise ValueError('Matrix does not match the researcher data')
    return len(files), len(checked)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--website-dir', type=Path, default=WEBSITE_DIR)
    args = parser.parse_args()
    files, details = validate_static_site(args.website_dir)
    print(f'PASS: {files} website files; {details} data references; Home and matrix agree.')


if __name__ == '__main__':
    main()
