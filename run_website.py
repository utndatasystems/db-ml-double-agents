#!/usr/bin/env python3
"""Serve the already-generated static website locally.

This script intentionally does not refresh data. Use ``run_pipeline.py`` first
when conference-service, identity-resolution, publication, or generated website
data may be stale.
"""

from __future__ import annotations

import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from scripts.site_paths import WEBSITE_DIR


ROOT = Path(__file__).resolve().parent
REQUIRED_FILES = (
    WEBSITE_DIR / "index.html",
    WEBSITE_DIR / "all.html",
    WEBSITE_DIR / "people.html",
    WEBSITE_DIR / "data" / "home_summary.json",
    WEBSITE_DIR / "data" / "site_data.js",
    WEBSITE_DIR / "data" / "matrix_stats.js",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the already-generated DB x ML website locally."
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Interface to bind to (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to serve on (default: 8000).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    missing = [path for path in REQUIRED_FILES if not path.exists()]
    if missing:
        print("ERROR: generated website files are missing:")
        for path in missing:
            try:
                display = path.relative_to(ROOT)
            except ValueError:
                display = path
            print(f"  - {display}")
        print()
        print(r"Run: python .\run_pipeline.py")
        return 1

    os.chdir(WEBSITE_DIR)
    server = ThreadingHTTPServer((args.host, args.port), SimpleHTTPRequestHandler)

    display_host = "localhost" if args.host in {"127.0.0.1", "localhost"} else args.host
    print(f"Website: http://{display_host}:{args.port}")
    print("Serving existing website data only. Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
