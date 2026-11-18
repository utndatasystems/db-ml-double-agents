"""Stage the current static dataset without retained generations for Pages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

try:
    from .validate_static_site import data_references, local_file, validate_static_site, wrapper
    from .site_paths import WEBSITE_DIR
except ImportError:
    from validate_static_site import data_references, local_file, validate_static_site, wrapper
    from site_paths import WEBSITE_DIR


PAGES_MAX_BYTES = 1_000_000_000


def prepare(source: Path, output: Path) -> int:
    source, output = source.resolve(), output.resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('The staging directory must be separate from the source directory')
    if output.exists():
        raise ValueError(f'Staging directory already exists: {output}')

    site = wrapper(local_file(source, 'data/site_data.js'), 'window.CROSSOVER_DATA=')
    coverage = json.loads(local_file(source, 'data/coverage_summary.json').read_text(encoding='utf-8'))
    pending = set(data_references(site)) | set(data_references(coverage))
    current = set()
    while pending:
        url = pending.pop()
        path = local_file(source, url)
        if path in current:
            continue
        current.add(path)
        pending.update(data_references(json.loads(path.read_text(encoding='utf-8'))))

    files = []
    for path in source.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Symlinks are not supported: {path}')
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        generated_detail = (
            relative.parts[:2] in {('data', 'people'), ('data', 'coverage')}
            or (relative.parent == Path('data') and path.name.startswith('publication_search.'))
        )
        if generated_detail and path not in current:
            continue
        files.append(path)

    size = sum(path.stat().st_size for path in files)
    if size >= PAGES_MAX_BYTES:
        raise ValueError(f'Current website is {size:,} bytes; Pages requires less than 1 GB')
    output.mkdir(parents=True)
    for path in files:
        destination = output / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return size


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=WEBSITE_DIR)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    validate_static_site(args.source)
    size = prepare(args.source, args.output)
    validate_static_site(args.output)
    print(f'Validated Pages bundle: {size:,} bytes in {args.output}')


if __name__ == '__main__':
    main()
