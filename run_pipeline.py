#!/usr/bin/env python3

"""Refresh the complete DB x ML dataset and rebuild the website.



Normal use:



    python run_pipeline.py



The pipeline refreshes the official DB-service pages, validates them, updates

identity resolution only when its semantic inputs changed, refreshes/reuses the

DBLP publication scan, resolves DBLP publication versions into conservative work

entities, rebuilds website data, and runs the tests. arXiv metadata is cached by


default; pass --refresh-arxiv when you explicitly want to re-fetch it.



Online runs discover the latest monthly DBLP and ROR releases and record their
versions and checksums in data/snapshot_sources.json. Use --keep-snapshots to
reuse that selection or --offline to avoid all source requests. --full-refresh
also forces identity/publication scans and refreshes supporting metadata.

"""



from __future__ import annotations



import argparse

from contextlib import closing

import hashlib

import json

import os

import shutil

import sqlite3

import subprocess

import sys

import time

from pathlib import Path

from typing import Sequence

from scripts.snapshot_sources import MANIFEST_ENV, MANIFEST_FILENAME, load_snapshot_sources
from scripts.site_paths import WEBSITE_DIR, BUILD_EXPORT_DIR



ROOT = Path(__file__).resolve().parent

DATA = ROOT / "data"

WEBSITE = WEBSITE_DIR

STATE_FILE = ".pipeline_state.json"



SCRAPERS = (

    "scrape_cidr_reviewers.py",

    "scrape_edbt_reviewers.py",

    "scrape_icde_reviewers.py",

    "scrape_sigmod_reviewers.py",

    "scrape_vldb_reviewers.py",

)



REVIEWER_DBS = (

    "cidr_reviewers.sqlite",

    "edbt_reviewers.sqlite",

    "icde_reviewers.sqlite",

    "sigmod_reviewers.sqlite",

    "vldb_reviewers.sqlite",

)



# If any of these files changes, force a fresh DBLP candidate index before

# applying the encoded entity-resolution rules again.

IDENTITY_RULE_FILES = (

    "scripts/enrich_reviewers_with_dblp.py",

    "scripts/apply_high_precision_entity_resolution.py",

    "scripts/apply_ror_entity_resolution.py",

    "scripts/apply_next_tier_entity_resolution.py",

    "scripts/apply_affiliation_entity_resolution.py",

    "scripts/apply_profile_entity_resolution.py",

    "data/reviewed_person_identities.csv",

    "data/reviewed_service_aliases.csv",

    "scripts/identity_review.py",

    "scripts/audit_unresolved_entity_resolution.py",

    "scripts/ror_affiliations.py",

    "scripts/snapshot_sources.py",

    "scripts/scraper_repairs.py",

    "scripts/service_roles.py",

)



# Re-fetching an unchanged page should not invalidate identity state merely

# because its fetch timestamp changed.

NON_SEMANTIC_REVIEWER_COLUMNS = {

    "fetched_at",

    "created_at",

    "updated_at",

    # EDBT links reviewer rows to a source_documents surrogate key. The source

    # URL is already part of the reviewer row, so this numeric pointer must not

    # make an unchanged scrape look semantically different.

    "source_page",

}





def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--full-refresh", action="store_true",
        help="Fetch latest monthly snapshots and rebuild every stage, refreshing arXiv and affiliation metadata too.",
    )
    parser.add_argument(
        "--keep-snapshots", action="store_true",
        help="Keep the recorded DBLP/ROR releases instead of discovering newer releases.",
    )
    parser.add_argument(
        "--refresh-affiliations", action="store_true",
        help="Re-fetch cached publisher metadata used for researcher affiliations.",
    )
    parser.add_argument(

        "--years",

        nargs="+",

        type=int,

        help=(

            "DB-service years to refresh. Omit to use each scraper's default "

            "range (2022 through the last completed calendar year)."

        ),

    )

    parser.add_argument(

        "--offline",

        action="store_true",

        help="Use existing local reviewer databases and pinned snapshots only.",

    )

    parser.add_argument(

        "--force-identity",

        action="store_true",

        help="Force a full DBLP person/candidate reindex.",

    )

    parser.add_argument(

        "--force-publications",

        action="store_true",

        help="Force a full DBLP publication rescan.",

    )

    parser.add_argument(

        "--refresh-arxiv",

        action="store_true",

        help="Re-fetch selected arXiv metadata even when it is already cached.",

    )

    parser.add_argument(

        "--verify-snapshots",

        action="store_true",

        help="Force full checksum verification of the pinned DBLP snapshot/DTD.",

    )

    parser.add_argument(

        "--review-entity-resolution",

        action="store_true",

        help=(

            "Generate guarded entity-resolution plans without applying them, "

            "then stop before rebuilding publication/site data."

        ),

    )

    parser.add_argument("--skip-tests", action="store_true")

    parser.add_argument("--serve", action="store_true")

    parser.add_argument("--port", type=int, default=8000)

    parser.add_argument(

        "--dry-run",

        action="store_true",

        help="Print the pipeline commands without executing them.",

    )

    args = parser.parse_args(argv)

    if args.full_refresh:
        if args.offline or args.keep_snapshots:
            parser.error("--full-refresh cannot be combined with --offline or --keep-snapshots.")
        args.force_identity = True
        args.force_publications = True
        args.refresh_arxiv = True
        args.refresh_affiliations = True
        args.verify_snapshots = True

    if args.offline and args.refresh_affiliations:
        parser.error("--refresh-affiliations cannot be used together with --offline.")
    if args.offline and args.refresh_arxiv:

        parser.error("--refresh-arxiv cannot be used together with --offline.")


    return args





def script(name: str) -> str:

    return str((ROOT / "scripts" / name).resolve())





def run(label: str, command: Sequence[str], *, dry_run: bool) -> None:

    print(f"\n=== {label} ===")

    print("$ " + subprocess.list2cmdline(list(command)))

    if dry_run:

        return

    env = dict(os.environ, **{MANIFEST_ENV: str(DATA.resolve() / MANIFEST_FILENAME)})
    result = subprocess.run(command, cwd=ROOT, env=env)

    if result.returncode:

        raise RuntimeError(f"{label} failed with exit code {result.returncode}")





def run_script(

    label: str,

    filename: str,

    *args: str,

    dry_run: bool,

) -> None:

    run(label, (sys.executable, script(filename), *args), dry_run=dry_run)





def reviewer_fingerprint(data_dir: Path) -> str:

    """Hash the reviewer rows actually consumed by entity resolution."""

    digest = hashlib.sha256()



    for filename in REVIEWER_DBS:

        path = data_dir / filename

        if not path.exists():

            raise FileNotFoundError(f"Missing reviewer database: {path}")



        conn = sqlite3.connect(str(path))

        try:

            columns = [

                str(row[1])

                for row in conn.execute("PRAGMA table_info(reviewer_records)")

                # row[5] is the SQLite primary-key flag. Scrapers delete and

                # reinsert year slices, so surrogate primary keys can change on

                # every refresh even when the actual reviewer data is identical.

                if not int(row[5])

                and str(row[1]) not in NON_SEMANTIC_REVIEWER_COLUMNS

            ]

            if not columns:

                raise RuntimeError(f"No reviewer_records columns found in {path}")



            quoted = ", ".join(f'"{column}"' for column in columns)

            rows = list(conn.execute(f"SELECT {quoted} FROM reviewer_records"))

            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, default=str))



            digest.update(filename.encode("utf-8"))

            digest.update(json.dumps(columns).encode("utf-8"))

            for row in rows:

                digest.update(

                    json.dumps(row, ensure_ascii=False, default=str).encode("utf-8")

                )

                digest.update(b"\n")

        finally:

            conn.close()



    return digest.hexdigest()





def identity_fingerprint(data_dir: Path) -> str:

    digest = hashlib.sha256()

    digest.update(reviewer_fingerprint(data_dir).encode("ascii"))

    # A source upgrade invalidates identity state even when service names and
    # matching code did not change. Check timestamps are intentionally excluded.
    digest.update(json.dumps(
        load_snapshot_sources(data_dir / MANIFEST_FILENAME), sort_keys=True,
    ).encode("utf-8"))
    for relative in IDENTITY_RULE_FILES:

        digest.update(relative.encode("utf-8"))

        rule_file = data_dir / relative[5:] if relative.startswith("data/") else ROOT / relative
        digest.update(rule_file.read_bytes() if rule_file.exists() else b"missing")

    return digest.hexdigest()





def load_state(data_dir: Path) -> dict[str, object]:

    path = data_dir / STATE_FILE

    if not path.exists():

        return {}

    try:

        value = json.loads(path.read_text(encoding="utf-8"))

    except (OSError, json.JSONDecodeError):

        return {}

    return value if isinstance(value, dict) else {}





def save_identity_state(data_dir: Path, fingerprint: str) -> None:

    path = data_dir / STATE_FILE

    path.write_text(

        json.dumps({"identity_fingerprint": fingerprint}, indent=2) + "\n",

        encoding="utf-8",

    )





def ror_is_local(data_dir: Path) -> bool:

    ror_dir = data_dir / "ror_snapshot"

    return (

        (ror_dir / "ror_affiliation_index.sqlite").exists()

        and any(ror_dir.glob("*-ror-data.zip"))

    )





def years_args(years: Sequence[int] | None) -> list[str]:

    if not years:

        return []

    return ["--years", *(str(year) for year in sorted(set(years)))]





def refresh_snapshots(args: argparse.Namespace, data_dir: Path) -> None:
    if args.offline or args.keep_snapshots:
        print("\nKeeping the recorded DBLP monthly and ROR snapshot selection.")
        return
    forwarded = ["--data-dir", str(data_dir)]
    if args.verify_snapshots:
        forwarded.append("--verify-snapshots")
    run_script(
        "Fetch latest monthly DBLP and ROR snapshots",
        "refresh_snapshots.py", *forwarded, dry_run=args.dry_run,
    )


def refresh_service_data(args: argparse.Namespace) -> None:

    if args.offline:

        print("\nOffline mode: keeping existing DB-service databases.")

    else:

        forwarded_years = years_args(args.years)

        for scraper in SCRAPERS:

            venue = scraper.removeprefix("scrape_").removesuffix("_reviewers.py").upper()

            run_script(

                f"Refresh {venue} service data",

                scraper,

                *forwarded_years,

                dry_run=args.dry_run,

            )



    run_script(

        "Validate DB-service coverage",

        "report_service_coverage.py",

        *years_args(args.years),

        dry_run=args.dry_run,

    )





def refresh_identity(args: argparse.Namespace, data_dir: Path) -> bool:

    """Refresh identity state when needed. Return True if it was rebuilt."""

    if args.dry_run:

        needed = True

        fingerprint = None

        reason = "dry run shows the conservative refresh path"

    else:

        fingerprint = identity_fingerprint(data_dir)

        previous = load_state(data_dir).get("identity_fingerprint")

        entity_db = data_dir / "entity_resolution.sqlite"

        needed = bool(

            args.force_identity

            or not entity_db.exists()

            or previous != fingerprint

        )

        if args.force_identity:

            reason = "forced by --force-identity"

        elif not entity_db.exists():

            reason = "entity_resolution.sqlite is missing"

        elif previous is None:

            reason = "no previous run_pipeline fingerprint exists"

        elif previous != fingerprint:

            reason = "reviewer data or identity-resolution rules changed"

        else:

            reason = "reviewer data and identity-resolution rules are unchanged"



    if not needed:

        print(f"\nIdentity refresh skipped: {reason}.")

        if args.review_entity_resolution:

            print(

                "Use --force-identity with --review-entity-resolution to "

                "regenerate the plans from scratch."

            )

        return False



    print(f"\nIdentity refresh required: {reason}.")

    # Back up through SQLite so committed WAL pages are included before the
    # candidate index and all resolution decisions are rebuilt.
    identity_db = data_dir / "entity_resolution.sqlite"
    if identity_db.exists() and not args.dry_run:
        backup = data_dir / "backups" / f"entity_resolution.before_identity_{time.time_ns()}.sqlite"
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(identity_db)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        print(f"Identity backup: {backup}")



    if args.offline:

        if not args.dry_run and not ror_is_local(data_dir):

            raise RuntimeError(

                "Offline identity refresh requires the pinned ROR snapshot/index."

            )

    else:

        run_script(

            "Ensure pinned ROR snapshot/index",

            "download_ror_snapshot.py",

            "--data-dir",

            str(data_dir),

            *(["--rebuild-index"] if args.full_refresh else []),

            dry_run=args.dry_run,

        )



    # Reindexing replaces accepted decisions before the guarded rules run.
    # A plan-only run or interruption must not leave an older success marker
    # that makes the next ordinary run skip those rules.
    if not args.dry_run:
        state = load_state(data_dir)
        state.pop("identity_fingerprint", None)
        state_path = data_dir / STATE_FILE
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    enrich_args = ["--data-dir", str(data_dir), "--reindex"]

    if args.offline:

        enrich_args.append("--no-download")

    if args.verify_snapshots:

        enrich_args.append("--verify-snapshot")

    run_script(

        "Resolve DB researchers against DBLP",

        "enrich_reviewers_with_dblp.py",

        *enrich_args,

        dry_run=args.dry_run,

    )



    apply_flag: list[str] = [] if args.review_entity_resolution else ["--apply"]

    for label, filename in (

        ("High-precision entity resolution", "apply_high_precision_entity_resolution.py"),

        ("ROR-backed entity resolution", "apply_ror_entity_resolution.py"),

        ("Next-tier entity resolution", "apply_next_tier_entity_resolution.py"),

        ("Complete affiliation entity resolution", "apply_affiliation_entity_resolution.py"),

        ("Service identity profile corrections", "apply_profile_entity_resolution.py"),

    ):

        run_script(

            label + (" (plan only)" if args.review_entity_resolution else " (apply)"),

            filename,

            "--data-dir",

            str(data_dir),

            *apply_flag,

            dry_run=args.dry_run,

        )



    run_script(

        "Audit unresolved identities",

        "audit_unresolved_entity_resolution.py",

        "--data-dir",

        str(data_dir),

        dry_run=args.dry_run,

    )



    if fingerprint is not None and not args.review_entity_resolution:

        save_identity_state(data_dir, fingerprint)

    return True





def refresh_publications(args: argparse.Namespace, data_dir: Path) -> None:

    scan_args = ["--data-dir", str(data_dir)]

    if args.force_publications:

        scan_args.append("--force-scan")

    if args.offline:

        scan_args.append("--no-download")

    if args.verify_snapshots:

        scan_args.append("--verify-snapshot")

    run_script(

        "Refresh DBLP publication corpus",

        "scan_dblp_publications.py",

        *scan_args,

        dry_run=args.dry_run,

    )





def refresh_publication_entities(args: argparse.Namespace, data_dir: Path) -> None:

    # First apply all evidence already cached locally. The resulting review queue

    # is then the minimal set that still needs arXiv enrichment.

    run_script(

        "Resolve DBLP publication entities",

        "resolve_publication_entities.py",

        "--data-dir",

        str(data_dir),

        dry_run=args.dry_run,

    )



    arxiv_args = ["--data-dir", str(data_dir), "--all-unresolved"]

    if args.offline:

        arxiv_args.append("--offline")

    elif args.refresh_arxiv:

        arxiv_args.append("--refresh")

    run_script(

        "Refresh arXiv publication evidence",

        "enrich_publication_candidates_with_arxiv.py",

        *arxiv_args,

        dry_run=args.dry_run,

    )



    # A live enrichment may have added new exact identifier/title evidence.

    # Re-run the resolver so website data always reflects the updated cache.

    run_script(

        "Apply arXiv publication evidence",

        "resolve_publication_entities.py",

        "--data-dir",

        str(data_dir),

        dry_run=args.dry_run,

    )



    # Rebuild the enriched audit view from the final unresolved queue. This is

    # cache-only even during an online pipeline run, so it cannot trigger a

    # second round of arXiv requests.

    run_script(

        "Rebuild final arXiv review report",

        "enrich_publication_candidates_with_arxiv.py",

        "--data-dir",

        str(data_dir),

        "--all-unresolved",

        "--offline",

        dry_run=args.dry_run,

    )





def build_site_args(

    data_dir: Path,

    website_data: Path,

    *,

    identity_refreshed: bool,

) -> list[str]:

    args = [

        "--data-dir",

        str(data_dir),

        "--output-dir",

        str(website_data),

    ]

    if not identity_refreshed:

        # The scrapers replace year slices even when their semantic contents are

        # identical, which advances the SQLite mtimes. run_pipeline has already

        # proven semantic equivalence via identity_fingerprint(), so bypass the

        # build script's conservative mtime-only guard for this case.

        args.append("--allow-stale-inputs")

    return args





def rebuild_website(

    args: argparse.Namespace,

    data_dir: Path,

    *,

    identity_refreshed: bool,

) -> None:

    website_data = WEBSITE / "data"

    affiliation_args = ["--data-dir", str(data_dir)]
    if args.offline:
        affiliation_args.append("--offline")
    elif args.refresh_affiliations:
        affiliation_args.append("--refresh")
    run_script(
        "Refresh automatic researcher affiliations",
        "refresh_affiliations.py",
        *affiliation_args,
        dry_run=args.dry_run,
    )

    run_script(

        "Build website data",

        "build_site_data.py",

        *build_site_args(

            data_dir,

            website_data,

            identity_refreshed=identity_refreshed,

        ),

        dry_run=args.dry_run,

    )

    run_script(

        "Build crossover matrix",

        "build_matrix_stats.py",

        "--entity-db",

        str(data_dir / "entity_resolution.sqlite"),

        "--site-data",

        str(BUILD_EXPORT_DIR / "site_data.json"),

        "--output-dir",

        str(website_data),

        dry_run=args.dry_run,

    )

    run_script(
        "Validate static website",
        "validate_static_site.py",
        "--website-dir", str(WEBSITE),
        dry_run=args.dry_run,
    )







def validate(args: argparse.Namespace) -> None:

    if args.skip_tests:

        return

    run(

        "Run Python tests",

        (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"),

        dry_run=args.dry_run,

    )

    node = shutil.which("node")

    if node:

        run(

            "Check JavaScript syntax",

            (node, "--check", str(WEBSITE / "assets" / "app.js")),

            dry_run=args.dry_run,

        )

    else:

        print("\nJavaScript syntax check skipped: Node.js is not installed.")





def main(argv: Sequence[str] | None = None) -> int:

    args = parse_args(argv)

    data_dir = DATA.resolve()

    data_dir.mkdir(parents=True, exist_ok=True)



    print("DB x ML pipeline")

    print("===============")

    print(f"Python:   {sys.executable}")

    print(f"Data:     {data_dir}")

    print(f"Website:  {WEBSITE.resolve()}")

    print(

        "Policy: latest monthly DBLP and ROR snapshots by default; "
        "selected releases are recorded for every downstream stage."

    )



    try:

        refresh_snapshots(args, data_dir)

        refresh_service_data(args)



        if not args.dry_run:

            missing = [data_dir / name for name in REVIEWER_DBS if not (data_dir / name).exists()]

            if missing:

                raise RuntimeError(

                    "Missing reviewer database(s):\n" + "\n".join(f"  - {p}" for p in missing)

                )



        refreshed_identity = refresh_identity(args, data_dir)

        if args.review_entity_resolution:

            if refreshed_identity:

                print(

                    "\nReview mode complete. Plans were generated but not applied; "

                    "publication/site generation was intentionally skipped."

                )

            return 0



        refresh_publications(args, data_dir)

        refresh_publication_entities(args, data_dir)

        run_script(
            "Revalidate manual publication categories",
            "audit_publication_categories.py",
            "--data-dir", str(data_dir), dry_run=args.dry_run,
        )

        rebuild_website(

            args,

            data_dir,

            identity_refreshed=refreshed_identity,

        )

        validate(args)



        if args.dry_run:

            print("\nDry run complete; no commands were executed.")

            return 0



        print(f"\nPipeline complete. Fresh website data: {WEBSITE / 'data'}")

        if args.serve:

            print(f"Serving http://localhost:{args.port} (Ctrl+C to stop)")

            return subprocess.run(

                [sys.executable, "-m", "http.server", str(args.port)],

                cwd=WEBSITE,

            ).returncode

        return 0



    except KeyboardInterrupt:

        print("\nPipeline interrupted.", file=sys.stderr)

        return 130

    except (OSError, RuntimeError, sqlite3.Error) as exc:

        print(f"\nERROR: {exc}", file=sys.stderr)

        return 1





if __name__ == "__main__":

    raise SystemExit(main())
