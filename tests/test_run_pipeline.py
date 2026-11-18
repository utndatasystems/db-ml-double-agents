from __future__ import annotations

import sqlite3
import copy
import json
import contextlib
import io
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import run_pipeline
from scripts.snapshot_sources import LEGACY_SOURCES, MANIFEST_FILENAME, MANIFEST_ENV


class RunPipelineTests(unittest.TestCase):
    def test_refresh_badge_only_records_completed_full_refresh_with_tests(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            data_dir = Path(tmp)
            for name in run_pipeline.REVIEWER_DBS:
                (data_dir / name).touch()
            stack.enter_context(mock.patch.object(run_pipeline, 'DATA', data_dir))
            for name in ('refresh_snapshots', 'refresh_service_data', 'refresh_identity',
                         'refresh_publications', 'refresh_publication_entities',
                         'run_script', 'rebuild_website'):
                stack.enter_context(mock.patch.object(run_pipeline, name, return_value=False))
            validation = stack.enter_context(mock.patch.object(run_pipeline, 'validate'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            badge = data_dir / 'refresh_badge.json'

            for args in (['--full-refresh', '--dry-run'], ['--full-refresh', '--skip-tests'], []):
                self.assertEqual(run_pipeline.main(args), 0)
                self.assertFalse(badge.exists())
            validation.side_effect = RuntimeError('validation failed')
            self.assertEqual(run_pipeline.main(['--full-refresh']), 1)
            self.assertFalse(badge.exists())
            validation.side_effect = None
            self.assertEqual(run_pipeline.main(['--full-refresh']), 0)
            self.assertRegex(json.loads(badge.read_text())['message'], r'^\d{4}-\d{2}-\d{2}$')

    def test_manual_category_reviews_are_revalidated_before_the_website_build(self):
        stages = []
        with contextlib.ExitStack() as stack:
            for name in ('refresh_snapshots', 'refresh_service_data', 'refresh_identity',
                         'refresh_publications', 'refresh_publication_entities', 'validate'):
                stack.enter_context(mock.patch.object(run_pipeline, name, return_value=False))
            stack.enter_context(mock.patch.object(run_pipeline, 'run_script',
                                side_effect=lambda label, script, *args, **kwargs: stages.append(script)))
            stack.enter_context(mock.patch.object(run_pipeline, 'rebuild_website',
                                side_effect=lambda *args, **kwargs: stages.append('website')))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(run_pipeline.main(['--dry-run', '--offline']), 0)
        self.assertEqual(stages, ['audit_publication_categories.py', 'website'])

    def _identity_fixture(self, data_dir: Path) -> None:
        with contextlib.closing(sqlite3.connect(data_dir / "entity_resolution.sqlite")) as conn:
            conn.execute("CREATE TABLE identity_result (decision TEXT)")
            conn.execute("INSERT INTO identity_result VALUES ('reviewed')")
            conn.commit()
        run_pipeline.save_identity_state(data_dir, "unchanged-inputs")

    def test_forced_review_invalidates_success_marker_before_reindex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._identity_fixture(data_dir)
            marker_seen_by_reindex = []

            def mutate_base_identity(label, filename, *args, **kwargs):
                if filename == "enrich_reviewers_with_dblp.py":
                    marker_seen_by_reindex.append(run_pipeline.load_state(data_dir))
                    with contextlib.closing(sqlite3.connect(data_dir / "entity_resolution.sqlite")) as conn:
                        conn.execute("UPDATE identity_result SET decision='base-only'")
                        conn.commit()

            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(run_pipeline, "identity_fingerprint", return_value="unchanged-inputs"), \
                    mock.patch.object(run_pipeline, "ror_is_local", return_value=True), \
                    mock.patch.object(run_pipeline, "run_script", side_effect=mutate_base_identity) as runner:
                run_pipeline.refresh_identity(run_pipeline.parse_args([
                    "--offline", "--force-identity", "--review-entity-resolution",
                ]), data_dir)
                self.assertEqual(marker_seen_by_reindex, [{}])
                self.assertEqual(run_pipeline.load_state(data_dir), {})
                runner.reset_mock()
                refreshed = run_pipeline.refresh_identity(run_pipeline.parse_args(["--offline"]), data_dir)

            self.assertTrue(refreshed, "A normal run must apply rules after a plan-only rebuild")
            self.assertTrue(any("--apply" in call.args for call in runner.call_args_list))
            self.assertEqual(run_pipeline.load_state(data_dir), {"identity_fingerprint": "unchanged-inputs"})

    def test_interrupted_forced_rebuild_cannot_reuse_success_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._identity_fixture(data_dir)

            def fail_after_reindex(label, filename, *args, **kwargs):
                if filename == "enrich_reviewers_with_dblp.py":
                    with contextlib.closing(sqlite3.connect(data_dir / "entity_resolution.sqlite")) as conn:
                        conn.execute("UPDATE identity_result SET decision='base-only'")
                        conn.commit()
                if filename == "apply_high_precision_entity_resolution.py":
                    raise RuntimeError("simulated interrupted identity rule")

            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(run_pipeline, "identity_fingerprint", return_value="unchanged-inputs"), \
                    mock.patch.object(run_pipeline, "ror_is_local", return_value=True), \
                    mock.patch.object(run_pipeline, "run_script", side_effect=fail_after_reindex):
                with self.assertRaisesRegex(RuntimeError, "simulated interrupted"):
                    run_pipeline.refresh_identity(run_pipeline.parse_args([
                        "--offline", "--force-identity",
                    ]), data_dir)
                self.assertEqual(run_pipeline.load_state(data_dir), {})
                with mock.patch.object(run_pipeline, "run_script") as runner:
                    refreshed = run_pipeline.refresh_identity(run_pipeline.parse_args(["--offline"]), data_dir)

            self.assertTrue(refreshed)
            self.assertTrue(any(call.args[1] == "enrich_reviewers_with_dblp.py" for call in runner.call_args_list))

    def test_identity_dry_run_preserves_completed_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._identity_fixture(data_dir)
            before = (data_dir / run_pipeline.STATE_FILE).read_bytes()
            with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(run_pipeline, "run_script"):
                run_pipeline.refresh_identity(run_pipeline.parse_args([
                    "--offline", "--dry-run", "--force-identity",
                ]), data_dir)
            self.assertEqual((data_dir / run_pipeline.STATE_FILE).read_bytes(), before)
            with contextlib.closing(sqlite3.connect(data_dir / "entity_resolution.sqlite")) as conn:
                self.assertEqual(conn.execute("SELECT decision FROM identity_result").fetchone()[0], "reviewed")

    def test_completed_unchanged_identity_skips_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self._identity_fixture(data_dir)
            with contextlib.redirect_stdout(io.StringIO()), \
                    mock.patch.object(run_pipeline, "identity_fingerprint", return_value="unchanged-inputs"), \
                    mock.patch.object(run_pipeline, "run_script") as runner:
                refreshed = run_pipeline.refresh_identity(run_pipeline.parse_args(["--offline"]), data_dir)
            self.assertFalse(refreshed)
            runner.assert_not_called()
            self.assertEqual(run_pipeline.load_state(data_dir), {"identity_fingerprint": "unchanged-inputs"})

    def test_full_refresh_forces_all_scans_and_supporting_metadata(self) -> None:
        args = run_pipeline.parse_args(["--full-refresh"])
        for name in ("force_identity", "force_publications", "refresh_arxiv", "refresh_affiliations", "verify_snapshots"):
            self.assertTrue(getattr(args, name), name)
        for incompatible in ("--offline", "--keep-snapshots"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                run_pipeline.parse_args(["--full-refresh", incompatible])

    def test_snapshot_discovery_runs_online_only(self) -> None:
        for options in ([], ["--offline"], ["--keep-snapshots"]):
            with mock.patch.object(run_pipeline, "run_script") as runner:
                run_pipeline.refresh_snapshots(run_pipeline.parse_args(options), Path("data"))
            self.assertEqual(runner.call_count, 0 if options else 1)

    def test_source_changes_invalidate_identity_without_timestamp_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(run_pipeline, "reviewer_fingerprint", return_value="unchanged"):
            root = Path(tmp)
            sources = copy.deepcopy(LEGACY_SOURCES)
            manifest = root / MANIFEST_FILENAME
            def write(stamp):
                manifest.write_text(json.dumps({"schemaVersion": 1, "checkedAt": stamp, "sources": sources}), encoding="utf-8")
            write("first check")
            before = run_pipeline.identity_fingerprint(root)
            write("second check")
            self.assertEqual(before, run_pipeline.identity_fingerprint(root))
            sources["dblp"]["md5"] = "a" * 32
            write("new snapshot")
            after_dblp = run_pipeline.identity_fingerprint(root)
            self.assertNotEqual(before, after_dblp)
            sources["ror"]["md5"] = "b" * 32
            write("new ROR")
            self.assertNotEqual(after_dblp, run_pipeline.identity_fingerprint(root))

    def test_subprocesses_receive_the_same_snapshot_manifest(self) -> None:
        with mock.patch.object(run_pipeline.subprocess, "run", return_value=mock.Mock(returncode=0)) as runner:
            run_pipeline.run("example", ["python", "example.py"], dry_run=False)
        self.assertEqual(runner.call_args.kwargs["env"][MANIFEST_ENV], str(run_pipeline.DATA.resolve() / MANIFEST_FILENAME))

    def test_full_refresh_forwards_affiliation_refresh(self) -> None:
        with mock.patch.object(run_pipeline, "run_script") as runner:
            run_pipeline.rebuild_website(run_pipeline.parse_args(["--full-refresh"]), Path("data"), identity_refreshed=True)
        self.assertIn("--refresh", runner.call_args_list[0].args)

    def test_years_args_are_sorted_and_deduplicated(self) -> None:
        self.assertEqual(
            run_pipeline.years_args([2026, 2025, 2026]),
            ["--years", "2025", "2026"],
        )

    def test_default_resolution_arguments_apply_guarded_rules(self) -> None:
        args = run_pipeline.parse_args([])
        self.assertFalse(args.review_entity_resolution)
        self.assertFalse(args.force_identity)
        self.assertFalse(args.force_publications)

    def test_review_mode_is_explicit(self) -> None:
        args = run_pipeline.parse_args(["--review-entity-resolution"])
        self.assertTrue(args.review_entity_resolution)

    def test_complete_affiliation_rule_is_applied_after_legacy_rules_and_before_audit(self) -> None:
        args = run_pipeline.parse_args(["--offline", "--dry-run"])
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(run_pipeline, "run_script") as runner:
            run_pipeline.refresh_identity(args, Path("data"))
        scripts = [call.args[1] for call in runner.call_args_list]
        self.assertEqual(scripts[-3:], ["apply_affiliation_entity_resolution.py", "apply_profile_entity_resolution.py", "audit_unresolved_entity_resolution.py"])
        self.assertIn("--apply", runner.call_args_list[-3].args)
        self.assertIn("--apply", runner.call_args_list[-2].args)
        self.assertIn("scripts/apply_affiliation_entity_resolution.py", run_pipeline.IDENTITY_RULE_FILES)
        self.assertIn("scripts/audit_unresolved_entity_resolution.py", run_pipeline.IDENTITY_RULE_FILES)
        self.assertIn("scripts/apply_profile_entity_resolution.py", run_pipeline.IDENTITY_RULE_FILES)
        self.assertIn("data/reviewed_person_identities.csv", run_pipeline.IDENTITY_RULE_FILES)

    def test_review_mode_does_not_apply_profile_corrections(self) -> None:
        args = run_pipeline.parse_args(["--offline", "--dry-run", "--review-entity-resolution", "--force-identity"])
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(run_pipeline, "run_script") as runner:
            run_pipeline.refresh_identity(args, Path("data"))
        profile_call = next(c for c in runner.call_args_list if c.args[1] == "apply_profile_entity_resolution.py")
        self.assertNotIn("--apply", profile_call.args)

    def test_reviewed_identity_changes_invalidate_the_configured_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(run_pipeline, "reviewer_fingerprint", return_value="same"):
            root = Path(tmp)
            before = run_pipeline.identity_fingerprint(root)
            (root / "reviewed_person_identities.csv").write_text("new reviewed evidence", encoding="utf-8")
            self.assertNotEqual(before, run_pipeline.identity_fingerprint(root))

    def test_reviewed_service_alias_changes_invalidate_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(run_pipeline, "reviewer_fingerprint", return_value="same"):
            root = Path(tmp)
            before = run_pipeline.identity_fingerprint(root)
            (root / "reviewed_service_aliases.csv").write_text("reviewed spelling evidence", encoding="utf-8")
            self.assertNotEqual(before, run_pipeline.identity_fingerprint(root))

    def test_reviewer_fingerprint_ignores_database_internal_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            for filename in run_pipeline.REVIEWER_DBS:
                conn = sqlite3.connect(data_dir / filename)
                conn.execute(
                    """
                    CREATE TABLE reviewer_records (
                        id INTEGER PRIMARY KEY,
                        year INTEGER,
                        role TEXT,
                        name TEXT,
                        affiliation TEXT,
                        source_page INTEGER,
                        fetched_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO reviewer_records
                    (id, year, role, name, affiliation, source_page, fetched_at)
                    VALUES (1, 2025, 'PC', 'Ada Example', 'Example U', 10, 'old')
                    """
                )
                conn.commit()
                conn.close()

            before = run_pipeline.reviewer_fingerprint(data_dir)

            # Mimic a scraper replacing a year: the semantic row is identical,
            # but SQLite/source surrogate ids and fetch timestamps differ.
            for filename in run_pipeline.REVIEWER_DBS:
                conn = sqlite3.connect(data_dir / filename)
                conn.execute("DELETE FROM reviewer_records")
                conn.execute(
                    """
                    INSERT INTO reviewer_records
                    (id, year, role, name, affiliation, source_page, fetched_at)
                    VALUES (99, 2025, 'PC', 'Ada Example', 'Example U', 999, 'new')
                    """
                )
                conn.commit()
                conn.close()

            self.assertEqual(before, run_pipeline.reviewer_fingerprint(data_dir))

            conn = sqlite3.connect(data_dir / run_pipeline.REVIEWER_DBS[0])
            conn.execute("UPDATE reviewer_records SET affiliation='Changed U'")
            conn.commit()
            conn.close()
            self.assertNotEqual(before, run_pipeline.reviewer_fingerprint(data_dir))


    def test_publication_entity_resolution_refreshes_arxiv_between_resolver_passes(self) -> None:
        args = run_pipeline.parse_args([])
        with mock.patch.object(run_pipeline, "run_script") as runner:
            run_pipeline.refresh_publication_entities(args, Path("data"))
        self.assertEqual(
            runner.call_args_list,
            [
                mock.call(
                    "Resolve DBLP publication entities",
                    "resolve_publication_entities.py",
                    "--data-dir",
                    "data",
                    dry_run=False,
                ),
                mock.call(
                    "Refresh arXiv publication evidence",
                    "enrich_publication_candidates_with_arxiv.py",
                    "--data-dir",
                    "data",
                    "--all-unresolved",
                    dry_run=False,
                ),
                mock.call(
                    "Apply arXiv publication evidence",
                    "resolve_publication_entities.py",
                    "--data-dir",
                    "data",
                    dry_run=False,
                ),
                mock.call(
                    "Rebuild final arXiv review report",
                    "enrich_publication_candidates_with_arxiv.py",
                    "--data-dir",
                    "data",
                    "--all-unresolved",
                    "--offline",
                    dry_run=False,
                ),
            ],
        )


    def test_offline_publication_enrichment_does_not_call_arxiv_network(self) -> None:
        args = run_pipeline.parse_args(["--offline"])
        with mock.patch.object(run_pipeline, "run_script") as runner:
            run_pipeline.refresh_publication_entities(args, Path("data"))
        self.assertIn(
            mock.call(
                "Refresh arXiv publication evidence",
                "enrich_publication_candidates_with_arxiv.py",
                "--data-dir",
                "data",
                "--all-unresolved",
                "--offline",
                dry_run=False,
            ),
            runner.call_args_list,
        )

    def test_cached_identity_build_bypasses_mtime_only_freshness_guard(self) -> None:
        args = run_pipeline.build_site_args(
            Path("data"),
            Path("website/deploy/data"),
            identity_refreshed=False,
        )
        self.assertIn("--allow-stale-inputs", args)

        refreshed_args = run_pipeline.build_site_args(
            Path("data"),
            Path("website/deploy/data"),
            identity_refreshed=True,
        )
        self.assertNotIn("--allow-stale-inputs", refreshed_args)

    def test_automatic_affiliations_refresh_before_website_build(self) -> None:
        for arguments in [[], ["--offline"]]:
            args = run_pipeline.parse_args(arguments)
            with mock.patch.object(run_pipeline, "run_script") as runner:
                run_pipeline.rebuild_website(args, Path("data"), identity_refreshed=False)
            calls = runner.call_args_list
            self.assertEqual(calls[0].args[1], "refresh_affiliations.py")
            self.assertEqual(calls[1].args[1], "build_site_data.py")
            self.assertEqual(calls[2].args[1], "build_matrix_stats.py")
            self.assertEqual(calls[3].args[1], "validate_static_site.py")
            self.assertEqual(len(calls), 4)
            self.assertIn(str(run_pipeline.BUILD_EXPORT_DIR / "site_data.json"), calls[2].args)
            self.assertEqual("--offline" in calls[0].args, args.offline)


if __name__ == "__main__":
    unittest.main()
