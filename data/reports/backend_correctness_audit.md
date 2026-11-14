# Backend correctness audit

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../docs/PIPELINE.md) for current behavior.

Later investigation: [round two](backend_correctness_audit_round2.md).

<details>
<summary>Detailed audit evidence, fixes and limitations (November 2026)</summary>

Audit: 6–7 November 2026. Branch: `audit/backend-correctness-2026-11-06`.

## Result

Five implementation bugs were fixed in five local commits. The existing data still produces **833 DB-focused people, 227 main-ML authors and 533 distinct main-conference works**. No identity was forced, no threshold was changed and no website design or production dataset was edited. Nothing was pushed.

The fixes are verified by regression tests and cross-review. They do not establish that every scraped person, paper or identity is correct. Two known corrections still need a controlled data/export refresh; see the open items below.

## Baseline and comparison

Baseline HEAD: `5d26903f79938f8c6a91b9abe5867726ccebb7bf`. Investigation started with that commit and `7e9e823`, then followed their pipeline, scraper, resolver and frontend dependencies.

Pinned sources: DBLP `dblp-2026-10-03`; ROR `v2.14-2026-10-06`. Website input fingerprint: `sha256:da07158e13aa0a7e259f573574ab17ef154c5edbce3643f7ea17869e1144d7db`.

| Unit | Before | After, same stored inputs |
| --- | ---: | ---: |
| Extracted service observations | 4,666 | 4,666 |
| Normalized name groups | 1,450 | 1,450 |
| Resolved distinct DBLP PIDs | 1,126 | 1,126 |
| Qualifying PC members | 1,114 | 1,114 |
| DB-focused people | 833 | 833 |
| DB-focused people with a main ML paper | 227 | 227 |
| Distinct main ML works | 533 | 533 |
| Main ML researcher–work pairs | 648 | 648 |

Counts are different units: one person can have several service names, memberships and papers; one paper can have several included authors. Service pages establish listed membership or leadership, not how many reviews someone actually completed.

The classifier was rerun against the existing canonical publication CSV with its person-specific ownership keys and cached snapshot metadata, in temporary outputs. Every candidate profile matched the current export: 833 DB-focused, 27 ML-focused, 29 Mixed and 225 Limited evidence. There were **zero classification changes**. This is stronger than merely comparing headline totals.

The snapshot manifest and deployed `site_data.js`, `home_summary.json` and `coverage_summary.json` retain their baseline SHA-256 hashes. No new dumps or full refresh were run. Counterfactual experiments used a database backup or temporary fixtures.

## 1. Confirmed bugs fixed and verified

### F1 — A review or interrupted rebuild could preserve a false success marker

**Severity: high. Confidence: high. Commit: `f1c5b08`.**

- Trigger: start from a successfully fingerprinted identity database; force a plan-only identity review, or interrupt a forced rebuild after reindexing.
- Before: `refresh_identity()` reindexed the live database but retained the previous successful fingerprint. A later ordinary run could skip the guarded identity rules because semantic inputs were unchanged.
- Evidence: `run_pipeline.py:603` reads the previous marker; `scripts/enrich_reviewers_with_dblp.py` rebuilds base decisions in `resolve_names()`. A read-only production backup replay changed 204 name groups, including 15 PID substitutions, and reduced distinct assigned PIDs from 1,126 to 970. Before the fix, the following ordinary pipeline call returned `False` with zero rule-script calls. These are counterfactual base-resolver results, not changes to the production dataset or a completed full review scan.
- Fix: invalidate the success marker after backup/ROR preflight and before reindexing (`run_pipeline.py:703`). Record success only after the applied rule chain completes. Dry runs and unchanged successful runs retain their behavior.
- Regression: `tests/test_run_pipeline.py:25` and `:54` cover review/retry and interruption/retry; additional checks cover dry runs and successful skipping.
- Cross-review: the identity specialist confirmed the failure path and challenged invalidation timing. The pipeline specialist reviewed the final patch and tests. No unresolved disagreement.

### F2 — Applied manual decisions could survive failed evidence revalidation

**Severity: medium. Confidence: high. Commit: `7eb4dd3`.**

- Trigger: an already-applied reviewed profile or service alias acquires an out-of-scope affiliation, loses its candidate/person, or depends on an invalidated anchor.
- Before: proposal rejection returned a held reason without clearing the existing reviewed PID. The module explicitly requires renewed review when these conditions change. The previous safeguard covered only a restored, known-unreliable short exact profile.
- Evidence: temporary changed-affiliation cases based on Anthony Tung and Antonis Deligiannakis retained their applied PIDs before the fix. All 15 profile reviews and two service-alias reviews are still within their evidence scope in the current production data; **no current stale decision was found**.
- Fix: defer only the module's own reviewed assignment, or the existing known-bad exact-profile case. Preserve unrelated accepted decisions. Plan dependent aliases against final anchor assignments, validate the plan atomically and remove only stale review-added candidate links (`scripts/apply_profile_entity_resolution.py:174`, `:209`, `:245`). Publication aliases are not expanded.
- Regression: `tests/test_profile_entity_resolution.py:261` onward covers stale evidence, preserved unrelated decisions, candidate cleanup, ordering, missing people, alias chains/cycles and atomic rejection.
- Cross-review: the publication specialist approved the conservative scope and requested chain/cycle/missing-person tests, which were added. No unresolved disagreement.

### F3 — PODS program chairs were omitted from officer-page extraction

**Severity: medium. Confidence: high. Commit: `0a4c379`.**

- Trigger: PODS program leadership appears only on the joint SIGMOD/PODS officer page, or the separate PC page lists the chair as an ordinary member.
- Before: the officer parser deliberately excluded PODS headings, although `home_population.PC_ROLES` includes `pods_program_chair`.
- Official evidence: [2022 officers](https://2022.sigmod.org/org_conference_officers.shtml) list Pablo Barceló as PODS Program Chair; [2024 officers](https://2024.sigmod.org/org_conference_officers.shtml) list Wim Martens. Their [2022](https://2022.sigmod.org/org_pods_pc.shtml) and [2024](https://2024.sigmod.org/org_pods_pc.shtml) PC pages do not preserve both chair roles. Current stored evidence gives Pablo only 2024 general-chair service; Wim already has qualifying PC service.
- Fix: an anchored PODS program-chair heading rule and institution-aware chair/member deduplication (`scripts/scrape_sigmod_reviewers.py:111`, `:535`). Administrative PODS offices were not added to PC eligibility.
- Measured counterfactual: Pablo already resolves to PID `29/5169`; his existing cached publications yield 17 qualifying DB works and 12 ML works, hence **Mixed**. Adding his verified chair role in memory increases PC candidates 1,114 → 1,115 and Mixed candidates 29 → 30; DB-focused people remain 833. Wim's role correction does not change eligibility. This has not been applied to stored data.
- Regression: `tests/test_sigmod_parser.py:15` onward uses the official heading/card structure and checks that different campuses/labs and separate SIGMOD roles survive.
- Cross-review: the pipeline specialist rejected an initial first-comma affiliation shortcut because it could erase campus distinctions. The revised whole-institution comparison and collision tests were approved. No unresolved disagreement.

### F4 — A person could inherit DB evidence from a version they did not author

**Severity: medium. Confidence: high. Commit: `935b575`.**

- Trigger: a merged work contains both a preprint and a DB conference paper, but the person authored only the preprint.
- Before: the resolver recorded `person_version_dblp_keys`, but the loader dropped them and the classifier inspected all component versions. A fixture with two such preprints and two PC service years incorrectly qualified as DB-focused with two DB papers.
- Evidence: the resolver explicitly chooses a person-authored display record (`scripts/resolve_publication_entities.py:919`). The current corpus has 117,357 researcher–work rows and 14 rows with partial component ownership. **Zero nonowned qualifying DB versions** were found, so this bug does not change the current cohort.
- Fix: preserve ownership keys (`scripts/build_site_data.py:1475`, `:1564`) and require them for classification evidence (`scripts/focus_population.py:201`, `:218`). Keep full component metadata for browsing. Explicit empty ownership counts nothing; legacy missing ownership proves only the selected record. Nonowned versions receive an explicit exclusion reason in the audit.
- Regression: `tests/test_focus_population.py:208`, `:237`; `tests/test_build_site_data.py:40`. The preprint-only fixture now has zero DB papers and is Limited evidence; an actually owned conference version still counts.
- Cross-review: the pipeline specialist confirmed the resolver's intended ownership contract. Initial compatibility proposals differed; both specialists accepted the conservative selected-record fallback. No unresolved disagreement.

### F5 — Affiliation diagnostics retained the broader pre-cohort counts

**Severity: low. Confidence: high. Commit: `2d0f10d`.**

- Trigger: cohort filtering follows `build_payload()`.
- Before: summary diagnostics still described all 1,126 matched people, while the generated review CSV described the retained 833. The exported summary reports 1,002 review rows, 181 conflicts and 80 low-confidence affiliations; the retained-cohort values are **742, 143 and 65**.
- Fix: recompute those three fields over the retained cohort in `scripts/focus_population.py:266`. This changes diagnostic metadata, not identity selection or the homepage population. No frontend consumer of these fields was found.
- Regression: `tests/test_focus_population.py:263` checks actual review-row generation and that the input summary is preserved.
- Cross-review: independently confirmed and approved by the publication and pipeline specialists. The production summary has not yet been regenerated.

## 2. Confirmed issues still open

1. **Stored service evidence and generated exports need refresh after F3/F5.** The parser is corrected, but the saved inputs still omit the PODS roles and the deployed diagnostic summary still has its old values. Source-page tables retain hashes rather than replayable HTML. Re-fetching those pages would change the audit inputs, so it was not mixed into the same-input comparison. A controlled follow-up should refresh SIGMOD 2022/2024, apply the normal identity stages and regenerate exports with the pinned DBLP/ROR snapshots. Verify the predicted +1 Mixed PC candidate rather than assuming more DB-focused people.
2. **Browser resize check remains unresolved.** The existing `tests/test_all_presentation.js:126` assertion, “Line layout redraws after resize, including when page size is fixed,” failed twice in the local headless iframe harness. Frontend files are identical to baseline. This is a confirmed test failure, not yet a diagnosed product bug. No visual workaround was introduced. The standalone Home-link suite passes.

## 3. Suspicious cases requiring external evidence

- **15 author rows have an ORCID stored against another local candidate PID.** Examples include Djellel Difallah (`conf/kdd/FanLFDT26`), Joseph M. Hellerstein (`journals/pacmhci/ShankarGHP24`) and Björn Þór Jónsson (`conf/mir/TranBDHHJHRSTZG26`), plus Dimitrios Skoutas records. Exact-name linkage may be correct and the ORCID tag may be wrong, or DBLP may contain split identities. The observations are high-confidence; an incorrect person–paper assignment is **not established**. No rows were dropped or identities reassigned. Full record keys and ORCIDs are in `audit_publications_evidence.json`. Check publisher author metadata and official ORCID/DBLP profiles before changing matching policy.
- **33 sparse exact profiles have alternative candidates.** This is an existing review queue, not 33 demonstrated errors. Examples include Adam Lee, Ahmed Aly and Ahmed Mahmood. Resolve with independent identity evidence, never publication volume or a desired focus label.

## Validation and reproduction

Use the repository virtual environment from the project root:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.venv\Scripts\python.exe -m unittest tests.test_run_pipeline tests.test_profile_entity_resolution tests.test_sigmod_parser tests.test_service_roles
.venv\Scripts\python.exe -m unittest tests.test_focus_population tests.test_build_site_data tests.test_publication_entity_resolution
```

- Baseline: **311 Python tests passed**, no failures/errors/skips.
- Final: **328 Python tests passed**, no failures/errors/skips.
- Browser model tests: **68 Home-trend checks passed** (pooled shares, window boundaries, CAGR and fitted growth).
- Browser integration: **90 Home-link checks passed**; the broader presentation suite stopped at the unresolved resize assertion above.
- All 129,279 raw author rows have valid one-based positions/signatures. Frontend positions are intentionally zero-based. For the study's 648 main authorships: Sole 1, First 26, Middle 383, Last 238; none has invalid order metadata.
- Service observations reconcile to saved source rows: CIDR 140, EDBT 560, ICDE 1,383, SIGMOD 1,439 and VLDB 1,144. Every accepted PID exists and remains a candidate; all name groups have a resolution decision. These checks cannot detect names the scraper never extracted.
- Export membership reconciles: 833 included plus 281 excluded-but-discoverable PC people equals 1,114 candidates. Main unique works are NeurIPS 247, ICML 135 and ICLR 151. Home annual shares and first-time counts independently match exported paper/person records; all **1,191 referenced JSON assets exist**.
- Reviewed stage ordering, fingerprints, snapshot reuse, publication freshness guards, Sankey units/record-list paths and annual/cumulative numerator/denominator code. This included missing totals, coauthor deduplication, calendar gaps, pooled denominators and zero-baseline growth handling; it was not a full independent re-extraction of the XML snapshot.

Local evidence files beside this report:

- `backend_correctness_baseline.json`: baseline counts, snapshots and artifact hashes.
- `backend_correctness_final_tests.json`: final Python results.
- `backend_correctness_export_checks.json`: independent export reconciliation.
- `backend_correctness_classifier_parity.json`: same-input classifier comparison.
- `backend_correctness_input_preservation.json`: baseline artifact hash comparison.
- `audit_publications_evidence.json`: partial ownership, author-order checks, ORCID cases and before/after boundary fixture.

Additional local reproduction scripts are ignored audit artifacts: `python -m data.reports.audit_publications_probe` reproduces F4 against baseline code and checks the actual corpus; `.venv\Scripts\python.exe data/reports/.agent3_probe.py` replays base identity resolution on a temporary backup. The latter now reports that the next normal identity refresh runs, rather than skips. These require the existing local data, including ignored snapshots/caches. Browser harnesses are temporary files under the local TEMP directory, not repository test-runner commands.

## Prioritized follow-up

1. Apply the controlled source/export refresh for the two PODS years; check each role and the +1 Mixed-candidate prediction. Rebuild diagnostic metadata and ownership fields at the same time. Keep source versions explicit.
2. Verify the 15 ORCID cases against primary sources; only then decide whether conflicting ORCID evidence needs a blocking rule.
3. Diagnose the headless resize failure independently of these backend commits.
4. Continue sampling sparse identity alternatives and nonresearch publication exclusions with exact primary evidence.

## 4. Areas not inspected

No independent full scan of all 13 million DBLP records, manual verification of every roster/member/paper, live re-scrape of all venue years, verification of every arXiv merge, external resolution of the ORCID cases, deployment/CDN atomicity check or exhaustive browser/device review was performed. No full refresh was run. The audit does not measure extraction recall against every original page, prove every listed member actually reviewed papers or validate the classifier as a measure of seniority.

## Local commits

- `f1c5b08` — invalidate identity success state before rebuilding decisions.
- `7eb4dd3` — revalidate applied identity reviews and dependent service aliases.
- `0a4c379` — capture PODS program chairs without merging distinct affiliations.
- `935b575` — require person-owned publication versions for focus classification.
- `2d0f10d` — scope affiliation diagnostics to the retained researcher cohort.

</details>
