#!/usr/bin/env python3
"""Download the pinned ROR dump and build the local affiliation-name index."""

from __future__ import annotations

import argparse
from pathlib import Path

from ror_affiliations import (
    DEFAULT_DATA_DIR,
    DEFAULT_ROR_DIRNAME,
    ROR_INDEX_FILENAME,
    ROR_RELEASE,
    ROR_ZIP_FILENAME,
    ensure_ror_index,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=f"Data directory (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Redownload the pinned ROR ZIP even if the local checksum is valid.",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Rebuild the derived SQLite affiliation index.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    ror_dir = args.data_dir.resolve() / DEFAULT_ROR_DIRNAME
    snapshot, index_path = ensure_ror_index(
        ror_dir,
        download=True,
        force_download=args.force_download,
        force_rebuild=args.rebuild_index,
    )
    print(f"ROR release:  {ROR_RELEASE}")
    print(f"Snapshot ZIP: {snapshot} ({ROR_ZIP_FILENAME})")
    print(f"Local index:  {index_path} ({ROR_INDEX_FILENAME})")
    print("Entity-resolution matching can now run fully offline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
