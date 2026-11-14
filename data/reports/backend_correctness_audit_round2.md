# Backend correctness audit, round two

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../docs/PIPELINE.md) for current behavior.

<details>
<summary>Detailed audit evidence, fixes and limitations (November 2026)</summary>

Completed 7 November 2026 on `audit/backend-correctness-2026-11-06`. Local commits only; nothing pushed. Website design and classification thresholds are unchanged.

## Outcome

Nine specialists investigated in three waves, with at most three specialists active together. They submitted read-only findings before editing and cross-reviewed diagnoses and fixes. The coordinator integrated shared-file changes, replayed the affected production stages and checked the promoted exports. This was a bounded audit, not verification of every person, paper or historical source page.

The audit fixed dropped affiliation-bearing service observations, one independently verified wrong identity, tutorial evidence entering the research-paper classifier and six persistence/ownership/recovery boundaries. The refreshed production exports retain the same **833 DB-focused people, 227 main-conference authors, 533 distinct main works and 648 researcher–work pairs**. Home's summary is byte-identical to the baseline. The broader identity and classification evidence changed as explained below.

**Validation:** 361 Python tests passed, versus 328 at baseline. Chrome checks passed: 182 presentation, 90 Home links, 68 trend-model and 782 Sankey checks. A separate current-export reconciliation found no failures across 125 inspection descriptors, 36 nodes and 37 ribbons, lazy person assets, publication search and stored source counts. This does not independently verify every downloaded CSV cell or every upstream identity.

## Baseline and fixed inputs

Baseline HEAD: `567b35bd11d7f9c686cb66570d00409e9cb0f35d`; branch as above; working tree initially clean. The previous five implementation fixes and `backend_correctness_audit.md` were reviewed rather than assumed correct. Its previously stale production outputs were included in this refresh.

- DBLP: `dblp-2026-10-03`, MD5 `cbd329100ea1bbb2873fa2f6d414ea00`.
- DTD: `dblp-2023-06-28`, MD5 `96e4283e71886dec2173718957293648`.
- ROR: `v2.14-2026-10-06`, MD5 `e16a5ceb8ff06f1a274dbd9288fe0159`.
- Snapshot-selection manifest SHA-256: `89585b497667b47fc287d842d431058b2a9a19522da2d3529b0b9a9a6fdf6019`, unchanged.
- Baseline website input fingerprint: `sha256:da07158e13aa0a7e259f573574ab17ef154c5edbce3643f7ea17869e1144d7db`.

Full baseline counts, artifact hashes and the passing 328-test result are in [round2/baseline.json](round2/baseline.json). Final hashes are in [round2/final_artifact_hashes.json](round2/final_artifact_hashes.json). Small external page checks resolved specific questions; no new large snapshot was downloaded.

## Specialists and adversarial cross-review

| Wave / specialist | Investigation and meaningful challenge | Review and resolution |
| --- | --- | --- |
| 1 / Source coverage | Same name is not sufficient grounds to discard different source affiliations. Replayed SIGMOD and PODS records; checked VLDB volume 18 board. | Identity specialist challenged affiliation variants and Jia Yu's downstream identity. Full normalized affiliations are retained without fuzzy merging; Jia was genuinely re-evaluated. Parser fix approved. |
| 1 / Identity ambiguity | Pittsburgh's Adam Lee was assigned to a sparse short-name PID. Independent institutional identity/publication evidence identifies Adam J. Lee. | Affiliation specialist challenged reliance on publication volume and redirect evidence. Scoped review uses the official roster, institutional identity and exact publication/coauthors, not volume. Review and tests approved; publication specialist also checked the correction. |
| 1 / Affiliations and reviews | Deleting an applied review leaves its decision active on direct invocation. Manual affiliation evidence could cite a nonowned version. | Identity specialist distinguished direct-stage failure from normal reindex behavior; numerical specialist required anchor-to-alias deletion coverage. Ownership reviewer required explicit-empty and legacy cases. Narrow fixes approved, without claiming current bad affiliations. |
| 2 / Publication identifiers | A reviewed author correction can add the target while retaining the automatic source at the same author position. Investigated all 15 ORCID contradictions. | Numerical specialist demanded exact-signature, rejected-review and genuine second-source-position controls. Added those tests; replacement suppresses only validated source/position pairs. No blanket ORCID reassignment. |
| 2 / Work deduplication | Preprint heuristics can transitively bridge two archival components after the initial guard was checked. | Focus specialist challenged dynamic union-root changes and preservation of stronger DOI/explicit links. Three original failing cases now pass; fixed-input comparison of all 117,357 selected person-work rows is identical. |
| 2 / Focus eligibility | Long official tutorials satisfy the six-page rule and contribute research evidence. | Deduplication specialist required exact official track/key evidence, especially PeiHTL08, rather than a broad title filter. Nine documented key exclusions approved; real-data labels unchanged. |
| 3 / Numerical and temporal | Independently recalculated annual shares, pooled shares, CAGR, fitted growth, first appearances and counting units. | Investigated apparent discrepancies between stored endpoint CAGR and displayed fitted growth; these are distinct documented metrics. No numerical implementation error established. Independently rechecked final core membership and all Home inputs after promotion. |
| 3 / Pipeline recovery | Supporting DOI-cache changes are absent from offline evidence reuse. Browser manifest is published before its assets. | Export specialist challenged WAL, missing cache, legacy migration, failed writes and open readers. Logical cache fingerprint and assets-first publication approved. Initial retention implementation failed identical-generation repetition; corrected before commit. |
| 3 / Exports and Sankeys | New coverage publication deletes assets needed by an open previous summary. Historical resize failure was also investigated. | Pipeline specialist reviewed failure/retry, malformed manifests and current/previous distinct generation retention. Fix approved. Resize passes without frontend changes; no invented root cause or workaround. |

These were interacting investigations, not nine independent endorsements. The coordinator returned objections, kept shared-file ownership, and reused existing specialists for post-fix review. No additional specialists were spawned. Initial reports and experiments remain distinguishable from final production evidence.

## Confirmed bugs fixed and verified

All findings below have high confidence in the stated implementation boundary and medium severity. Dataset impact is stated separately from fixture impact; an exposed boundary does not imply current data is wrong.

### F1 — Source merge loses different affiliations

`scripts/scrape_sigmod_reviewers.py:350` and `:535` previously collapsed side-track/global records using role/name without the full affiliation. Official SIGMOD 2024 lists Chao Zhang twice at different institutions; SIGMOD 2025 does the same for Chao Zhang and Jia Yu. Three actual affiliation-bearing observations were dropped even though their source bytes were already stored.

Fix: include normalized full affiliation in deduplication keys, preserving exact duplicate collapse and the existing institution-aware PODS chair/member guard. Regression fixtures fail on the original implementation. The source specialist's seven saved SIGMOD/PODS/officer pages hash-match stored `source_pages`, so the before/after is fixed-source, not a changed website.

Replaying those same pages also applies the previous audit's PODS officer fix: Pablo Barceló 2022 chair +1; Wim Martens 2024 member becomes chair, net 0; Benny Kimelfeld 2025 duplicate ordinary member is suppressed against his chair record, -1. Net across all sources: **4,666 → 4,669 observations**. Different affiliations are evidence, not automatically different people.

Evidence: [source_coverage_findings.md](round2/source_coverage_findings.md), [source_coverage_evidence_after_fix.json](round2/source_coverage_evidence_after_fix.json). Commit `219aaa1`.

### F2 — Pittsburgh Adam Lee assigned to the wrong PID

The unique-exact-alias base rule (`scripts/enrich_reviewers_with_dblp.py:1435`) accepted `58/8815`, while the service person is Adam J. Lee, `62/1373`. The [official SIGMOD roster](https://2022.sigmod.org/org_sigmod_pc.shtml), [Pittsburgh institutional page](https://people.cs.pitt.edu/~adamlee/), [institutional publication list](https://people.cs.pitt.edu/~adamlee/pubs/index.html) and [DBLP identity](https://dblp.org/pid/62/1373.html) corroborate this independently; `conf/ACMdis/JainFL22` supplies a specific title/coauthor cross-check.

Fix: one scoped row at `data/reviewed_person_identities.csv:17`, requiring the Pittsburgh evidence and the target candidate under existing validators. No automatic thresholds were relaxed and no publications were transferred from the old PID. Actual pinned rescan removes 25 old raw links and adds 110 independently matched target links. The corrected person remains Limited evidence: 2 DB papers, 0 ML papers, 2 publication years and only 1 PC service year.

Evidence: [identity_ambiguity_findings.md](round2/identity_ambiguity_findings.md), [identity_ambiguity_evidence.json](round2/identity_ambiguity_evidence.json). Commit `182df94`.

### F3 — Official tutorials counted as DB research papers

Selected-proceedings records of six or more pages can pass `scripts/focus_population.py:98` unless excluded. Neutral-title tutorials therefore supplied research evidence. The [official 2022 tutorial list](https://2022.sigmod.org/sigmod_tutorials.shtml), [2022 proceedings](https://dblp.org/db/conf/sigmod/sigmod2022.html) and [SIGMOD-hosted 2008 proceedings](https://sigmod.org/publications/dblp/db/conf/sigmod/sigmod2008.html) establish the exact keys.

Fix: nine exact-key exclusions at `data/focus_publication_exclusions.csv:25`, covering eight 2022 tutorials and PeiHTL08. Two already contributed nothing because of length/absence. The seven currently counted keys contributed **18 person-work pairs**; each affected candidate loses one DB paper, two also lose a publication year. No real focus label changes. Publications remain browseable. Same-title unrelated research still qualifies; no broad lexical exclusion was added.

Evidence: [focus_findings.md](round2/focus_findings.md), [focus_evidence.json](round2/focus_evidence.json), [focus_final_evidence.json](round2/focus_final_evidence.json). Commit `f6e868a`. Historical track coverage remains bounded; see open questions.

### F4 — Deleted review decisions survive direct-stage execution

`scripts/apply_profile_entity_resolution.py:164` previously iterated configured reviews without revoking module-owned statuses whose review row disappeared. Removing all review inputs in a read-only counterfactual left all 17 applied assignments uncleared. **No actual current missing review row was found.** Normal pipeline reindexing masks this direct-invocation failure.

Fix: preplan deferral of owned reviewed statuses absent from both review configurations, clear projected anchors before dependent aliases are planned, and remove only review-added candidate links. Do not restore an unreliable previous exact PID or alter unrelated decisions. Regression tests include deleted profile-to-retained-alias cascades, ordering and idempotence. A future normal full reindex still applies its ordinary guarded automatic rules; this is not a permanent never-match tombstone.

Evidence: [affiliation_review_findings.md](round2/affiliation_review_findings.md) and its [probe output](round2/affiliation_review_evidence.json). Commit `8df96ed`.

### F5 — Reviewed author replacement double-counts its source

`scripts/scan_dblp_publications.py:761` now suppresses only `(source PID, author position)` automatic matches actually replaced by a validated reviewed target. Previously the source and target could both acquire one author position when both were resolved service people. Other genuine source coauthor positions remain; contradictory PID/ORCID and ambiguous signatures do not authorize suppression. Scan cache version is **7**, ensuring prior results cannot bypass the new logic.

The fixture changes two person-paper rows to the intended one. Both current Farhana review rows have an unresolved source PID, so this bug has **zero demonstrated current-record impact**. Six boundary tests cover direct PID/signature replacement, another real source position and rejected corrections.

Evidence: [publication_identifiers_findings.md](round2/publication_identifiers_findings.md), [publication_identifiers_evidence.json](round2/publication_identifiers_evidence.json). Commit `004b941`.

### F6 — Heuristic preprint links bridge archival components

`scripts/resolve_publication_entities.py:338` now tracks archival membership on current union roots each time an exact-title, version-marker or arXiv heuristic is applied. A prior successful join can otherwise invalidate an earlier membership check and bridge two archival papers. Strong explicit relations/shared DOI links retain their existing behavior.

Three original regression cases fail on baseline and pass after the guard. Recomputing the entire **fixed original** raw CSV under both implementations produces identical statistics and all 117,357 selected person-work rows, including key/year/venue/position/ownership. Thus no current production count change is attributed to this fix. A conflicting external-target ordering ambiguity remains possible and is not claimed fixed.

Evidence: [work_dedup_full_verification.json](round2/work_dedup_full_verification.json), [work_dedup_evidence.json](round2/work_dedup_evidence.json). Commit `8d3b737`.

### F7 — Manual affiliation evidence accepts a nonowned version

`scripts/build_site_data.py:1649` previously treated every version in a merged work as authored by the person. A fixture let Alice's own preprint plus someone else's conference version validate a manual Coauthor University affiliation.

Fix: validate against `personVersionDblpKeys`; explicit empty ownership allows none; legacy missing ownership permits only the selected record. Preserve merged metadata for browsing. All four current manual rows are unaffected, and no current bad display affiliation was demonstrated. Automatic DOI candidate serialization has a related remaining guard gap, disclosed below.

Evidence: [affiliation_review_evidence.json](round2/affiliation_review_evidence.json), [work_ownership_export_verification.json](round2/work_ownership_export_verification.json). Commit `38a7eae`.

### F8 — Offline affiliation refresh ignores changed supporting cache

`scripts/refresh_affiliations.py:19` adds a logical hash of committed `(url, fetched_at, status, payload)` response rows, stored in top-level `metadataCache`. Offline reuse now checks this along with primary inputs/policy. The read-only SQLite query includes committed WAL and is stable through VACUUM. Missing-whole-cache behavior preserves a valid prior snapshot without creating an empty cache; legacy snapshots rebuild when a cache exists.

Fixed-input fixture: restored local DOI evidence gives **0 candidates on original ordinary retry versus 1 on forced rebuild**; fixed ordinary retry and forced rebuild both give 1. None of the current previously unavailable checks had recovered positive cache evidence, so actual recovered-candidate impact is **0 demonstrated**. Eight tests cover cache restoration, explicit negatives, missing/legacy cases, WAL, VACUUM and repeat stability. Partial cache row loss still follows the existing available-evidence policy.

Evidence: [agent8_pipeline_recovery.md](round2/agent8_pipeline_recovery.md), [pipeline_fix_verification.json](round2/pipeline_fix_verification.json). Commit `071c083`.

### F9 — Interrupted publication and cleanup break manifest readers

`scripts/build_site_data.py:2422` now writes immutable detail/search assets before atomically promoting `site_data.js`; `scripts/data_coverage.py:481` applies the same ordering and bounded retention to coverage summaries. Original browser ordering exposed two missing new assets on injected interruption; original cleanup removed the previous coverage list still held by an open tab. No missing reference was demonstrated in the original completed production export.

Fix: retain current and preceding **distinct asset generation**, publish before cleanup, preserve previous generation on identical/wording-only repetition, and disable pruning when an old manifest cannot safely be parsed. Tests inject detail, search and manifest write failures, then verify retry, three generations, identical repeats and malformed manifests. A cross-review caught an initial repeat-generation pruning gap; corrected before commit.

This is not an atomic transaction across all full JSON/Home/coverage/browser manifests, not multiwriter locking and not indefinite compatibility with arbitrarily old open tabs. Keeping a prior generation intentionally retains production assets. This refresh adds 1,157 immutable assets (about 542.5 MB uncompressed, largest about 57.5 MB); these are deployed production data, not experimental dumps.

Evidence: [exports_findings.md](round2/exports_findings.md), [pipeline_probe.json](round2/pipeline_probe.json), tests in `tests/test_build_site_data.py` and `tests/test_data_coverage.py`. Commit `2bc7a44`.

## Production refresh and complete count accounting

Production refresh is **complete**, not pending. The same pinned source snapshots were retained. No normal full download pipeline was started.

1. Backed up production inputs to ignored local `data/backups/round2_20261107T155840Z` using SQLite backups where applicable.
2. Replayed corrected SIGMOD/PODS source parsing on the seven hash-identical pages in private staging. Asserted the name set remained identical before reusing the existing pinned candidate index. Reset base decisions and ran all five guarded identity-rule stages with the same ROR index and reviewed inputs.
3. Adopted the verified SIGMOD and identity databases, re-exported resolution diagnostics and cleared the normal pipeline identity success marker conservatively. The controlled partial replay is not falsely recorded as a completed normal full pipeline; the next normal invocation can recompute its own fingerprint.
4. Ran publication scanner with `--no-download`. Cache v7 correctly required a real scan of existing XML, rather than relabeling a v6 result. Rebuilt canonical works using the existing arXiv cache, then offline affiliations with existing DOI responses: zero requests/errors.
5. Built website and matrix exports in staging with normal freshness validation. Checked all 1,191 referenced assets, published immutable assets first and six manifests atomically one at a time, retained prior assets, then ran final browser and independent data checks.

Two temporary staging attempts encountered Windows text-encoding errors; name-equality guards stopped them before adoption. Explicit UTF-8 reads/writes/subprocess encoding corrected those experiments. Neither failed staging result was promoted.

| Measure / counting unit | Before | After | Explanation |
| --- | ---: | ---: | --- |
| Service observations | 4,666 | 4,669 | Three restored affiliation observations, Pablo chair +1, Benny duplicate -1; Wim role changes only. |
| Normalized name groups | 1,450 | 1,450 | No new normalized name. |
| Resolved unique PIDs | 1,126 | 1,125 | Adam swaps PID; Jia Yu no longer passes complete affiliation coverage. |
| Matched qualifying PC candidates | 1,114 | 1,114 | Add Pablo and corrected Adam; remove old Adam and held Jia. |
| DB-focused | 833 | 833 | Exact included PID set unchanged. |
| ML-focused | 27 | 27 | No label change. |
| Mixed | 29 | 30 | Newly recovered Pablo: 17 DB, 12 ML, 18 publication years. |
| Limited evidence | 225 | 224 | Adam replacement remains Limited; Jia removed. |
| Raw person–record links | 129,279 | 129,362 | New Adam +110, old Adam -25, Jia -2. |
| Distinct raw DBLP records | 96,838 | 96,906 | Result of those identity changes. |
| Canonical works, all resolved people | 88,096 | 88,166 | Same fixed snapshot, changed identity targets. |
| Canonical person–work links | 117,357 | 117,442 | New Adam +109, old Adam -22, Jia -2. |
| Core main-conference authors | 227 | 227 | Exact membership unchanged. |
| Core main-conference works | 533 | 533 | Exact selected work/key/venue/year membership unchanged. |
| Core main-conference authorships | 648 | 648 | Exact PID/work pairs unchanged. |

No common identity's canonical selected records, dates, venues, positions or ownership change outside the three changed PIDs. The resolver-only fixed-input comparison separately establishes zero deduplication impact on the old corpus. Focus-paper exclusions alter 18 common profiles' evidence counts but no labels; additions/removals bring the total candidate-profile deltas to 22.

Jia Yu's new Wherobots observation invalidates the former complete-coverage match to `86/3457-20`; the result is now `ambiguous_exact_name` with 19 real candidate identities. The [personal page](https://jiayuasu.github.io/) links Wherobots and prior Washington State service, while live DBLP exposes split-profile complications. This is a conservative held decision, not a conclusion that the service person is unrelated to either profile. No target was forced merely to preserve totals. Chao Zhang also remains ambiguous.

The prior report's stale affiliation diagnostics are now refreshed for the retained cohort: **143 conflicts, 65 low-confidence cases, 742 review rows**, rather than 181/80/1,002. Core displayed affiliations are unchanged. Offline evidence still covers 775 people. The only common-person automatic snapshot difference is the display name `Pablo Barcelo` → `Pablo Barceló`; candidate evidence, checks and years are identical. A repeated offline run reuses byte-identical evidence.

Home summary SHA-256 remains `ec51b48ac54963fd24cd1cef0a4d7485648717f083c4f9e8dd178f3d8b83837d`. All 75 annual points, ten first-time counts, venue/year denominators and runtime growth inputs are unchanged. Final 13-check focus and seven-check numerical reviews were performed by specialists on promoted files, preserving their original baseline evidence.

Detailed accounting: [identity_stage_result.json](round2/identity_stage_result.json), [publication_scan_delta.json](round2/publication_scan_delta.json), [publication_entities_delta.json](round2/publication_entities_delta.json), [final_export_delta.json](round2/final_export_delta.json), [final_affiliation_and_asset_check.json](round2/final_affiliation_and_asset_check.json), [exports_final_evidence.json](round2/exports_final_evidence.json).

## Calculation meaning and presentation findings

The independent numerical probe reads canonical CSV work/PID sets, rather than trusting Home summary builders:

- 2025 annual shares: NeurIPS 84/5,823 = 1.442555%; ICML 45/3,268 = 1.376989%; ICLR 47/3,704 = 1.268898%.
- Five-year 2021–2025 trends are pooled ratios: 213/19,025, 107/10,123 and 95/9,491. They are not unweighted means of percentages.
- The 2026 runtime window starts 2006; first positive annual baseline within the window gives CAGR 5.091040% for NeurIPS (2010–2025), 3.514951% for ICML (2006–2025), 5.149307% for ICLR (2018–2025). `website/assets/home-trends.js:26` calculates the displayed window; a historical backend field can legitimately describe a different baseline.
- First-time counts in 2016–2025 are 1, 2, 6, 15, 12, 18, 23, 28, 44, 52. Independent squared-error exponential fit gives 33.0888875% annual growth, SSE 74.7717285; endpoint CAGR is 55.1197293%. Home intentionally displays the fitted rate, not that endpoint field. Different methods are not a numerical inconsistency.
- Cumulative Relative through 2025: NeurIPS 247/30,610, ICML 135/16,483, ICLR 105/11,214. Missing total years are unavailable/excluded under the existing policy; Absolute retains observations. This policy was not changed.
- 2026 has 56 main author/work rows, 46 works and 43 authors, all ICLR; eight first appearances. Lifetime headlines include these. Home trends end at the last shared coverage year, 2025. No completeness guarantee follows solely from a positive denominator.

The cohort is selected using current snapshot history and 2022–2025 service, then followed retrospectively. These are not reconstructed historical DB-focused populations or proof of causal migration of the entire DB community. PC/review-board membership is listed service, not evidence of completed reviews. No new methodological rule or design change was imposed.

Sankey units intentionally change: service observations → names → identities; person-record links → records → works. Merged aliases/repeated observations and coauthorship/version accounting are not additional people or discarded research. Candidate CSV expansion is labeled separately and need not have one row per selected name. Historical aggregate-only rejections retain explicit unavailable-record metadata rather than inventing individual inspection rows.

## Confirmed guard gaps still open

**Follow-up status, 7 November 2026:** Item 1 below is now fixed in the working tree and refreshed production exports. The complete SIGMOD 2017 tutorial track referenced in item 2 is now reviewed and excluded. The descriptions below preserve the original audit findings; see the follow-up at the end for verification and remaining evidence limits.

1. **Automatic affiliation DOI ownership filtering — medium severity, high confidence in exposure, no demonstrated wrong current affiliation.** `scripts/automatic_affiliations.py:38` considers same-year component DOIs, and the refresh serializer does not carry the full person-owned-version contract. Two stored rows expose unowned DOI candidates. Tim Kraska's older DOI is not checked by latest-year selection; Helen Huang's ADC DOI is checked but yields `no_author_affiliation`, no accepted candidate. The manual guard is fixed; the automatic path needs a separate serializer/policy-version change and end-to-end tests. See the exact rows in [affiliation_review_evidence.json](round2/affiliation_review_evidence.json).
2. **Incomplete historical nonresearch-key coverage — medium potential severity, high confidence that the bounded review is incomplete, uncertain additional impact.** The corrected 2022/2008 tutorial keys do not establish exclusion of all long tutorials, invited material or other nonresearch tracks. A lexical scan found 54 candidate titles, many legitimate research. SIGMOD 2017 `Kumar0017` has tutorial evidence needing an exact stored-record/official-track review; it was not included in the bounded reviewed list. Do not treat the new nine keys as exhaustive or broadly reject lexical matches.

These remain explicit follow-up work rather than silent changes to ownership or eligibility policy. No known current wrong automatic affiliation or additional changed focus label is asserted.

## Suspicious cases and unresolved external evidence

- **Jia Yu split-profile resolution:** high confidence that added evidence defeats the previous complete-coverage rule; uncertain correct consolidated publication ownership. Investigate the official homepage/PID linkage and publication-level evidence across the candidate profiles, then add a scoped persistent review only if warranted.
- **15 ORCID contradictions:** all are side-scope exact-signature matches; contradiction alone is not proof of wrong authorship. Djellel Difallah's publication list corroborates ownership despite a likely split DBLP identity. Joseph M. Hellerstein's manuscript corroborates Berkeley authorship while the tag points to Joseph L. Hellerstein. Several Skoutas manuscripts identify Athena, distinct from the conflicting Aegean ORCID owner. Seven additional Skoutas records and one Björn record remain externally unresolved. Preserve those flags; blanket ORCID replacement would discard corroborated correct assignments. Access-denied/timeouts are disclosed in the specialist report.
- **32 other sparse exact profiles:** not established wrong. Some may be split DBLP identities. Publication volume must not select targets.
- **Potential multiple-selected-key work deduplication:** current core main data has 533 works and 533 keys, no multi-key work. A global frontend key change is not justified without a counting-contract fixture.
- **Conflicting heuristic external targets:** the resolver guard prevents archival bridging, but a preprint component with contradictory target evidence can retain the first deterministic accepted target. No such multi-archival component was found in the actual fixed-input corpus; target-disambiguation policy was not redesigned.
- **Historical resize failure:** real prior failure, now unreproduced in unchanged frontend on repeated baseline/final runs. Scheduling is a hypothesis, not an established diagnosis. No timeout inflation or layout workaround was added.

## Methodological suggestions not implemented

Historical cohorts could answer whether the wider DB community changed over time, but that differs from following today's fixed qualifying cohort. Explicit venue/year completeness flags could improve temporal interpretation but require independently verified coverage. Source membership must continue to be described as membership rather than completed reviewing. A full historical track catalogue could replace incremental nonresearch-key review. None of these justify changing thresholds in this audit.

## Validation and reproduction

Run from repository root using the existing virtual environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe data/reports/round2/pipeline_fix_verification.py
.\.venv\Scripts\python.exe -m unittest tests.test_publication_entity_resolution tests.test_refresh_affiliations tests.test_data_coverage
```

The combined suite completed **361 tests, zero failures/errors/skips**. Boundary probes comparing against baseline code require commit `567b35b`; initial evidence files deliberately remain historical. The cache-comparison probe writes a separate `pipeline_fix_verification_rerun.json`. Other initial probes write their named evidence files; preserve them before rerunning. Before/after source probes depend on the ignored saved official pages. Final reconciliation can be rerun without overwriting baseline evidence:

```powershell
@'
from pathlib import Path
p = Path('data/reports/round2/exports_probe.py').resolve()
s = p.read_text(encoding='utf-8').replace('round2/exports_evidence.json', 'round2/exports_final_evidence.json')
exec(compile(s, str(p), 'exec'), {'__file__': str(p), '__name__': '__main__'})
'@ | .\.venv\Scripts\python.exe -
```

The minimal production sequence used the affected source/identity staging described above followed by:

```powershell
.\.venv\Scripts\python.exe scripts/scan_dblp_publications.py --data-dir data --no-download
.\.venv\Scripts\python.exe scripts/resolve_publication_entities.py --data-dir data
.\.venv\Scripts\python.exe scripts/refresh_affiliations.py --offline
.\.venv\Scripts\python.exe scripts/build_site_data.py --data-dir data --output-dir data/reports/round2/website_staged
```

The resolver reads the existing local arXiv cache by default; no network metadata refresh was used. Staging directories, large databases, raw/canonical CSVs and backups remain ignored local inputs rather than report-commit contents.

Chrome browser harnesses are the existing local `%TEMP%/dbml-check-pages-regression.py`, `dbml-audit-home-model.py` and `dbml-check-all-flow-components.py`, using repository JS tests and installed Chrome via loopback, without disabling the browser sandbox. Node was unavailable. Final results: **182 + 90 + 68 + 782 passed**. The first parallel final Sankey attempt returned WAIT without a result; it was not counted as passed. A standalone rerun completed all 782. Harmless navigation-related localhost connection aborts did not prevent completed checks. See [final_browser_checks.json](round2/final_browser_checks.json), [final_python_tests.json](round2/final_python_tests.json), [exports_browser_evidence.json](round2/exports_browser_evidence.json).

## Remaining limits and prioritized follow-up

1. Carry person-owned-version metadata through automatic affiliation inputs, filter DOI candidates conservatively and version the policy; test legacy serialization and current two exposed cases.
2. Resolve Jia Yu with primary publication/identity evidence; continue bounded sparse-profile and ORCID review without forcing candidate selections.
3. Review historical selected DB proceedings tracks, starting with identified long tutorials; maintain exact provenance-backed exclusions and measure boundary effects.
4. Inspect same-name source merge paths in ICDE/EDBT and remaining official rosters/alternate pages; no comparable production loss was established there in this pass.
5. If resize failure recurs, capture actual dimensions, document identity and timer/layout ordering before assigning cause. Consider deployment-generation consistency separately from local writer safety.

Not exhaustive: source recall and Shadow PC coverage across every venue/year; all identities/affiliations and every publisher author tag; every historical record type/channel; independent re-extraction of every conference-wide denominator; all CSV cells/split-edge identities; simultaneous writers/CDN transactions; all hidden-document scheduling states. Missing source names and ambiguous identities can still reduce coverage. Partial restoration of DOI cache rows can remove prior evidence under the unchanged available-evidence policy. One preceding asset generation protects bounded open readers, not tabs spanning unlimited future updates.

## Local commits

| Commit | Scope |
| --- | --- |
| `219aaa1` | Preserve distinct affiliations when merging committee entries. |
| `182df94` | Correct Pittsburgh Adam Lee service identity with scoped evidence. |
| `f6e868a` | Exclude verified SIGMOD tutorials from focus evidence. |
| `8df96ed` | Revoke reviewed identity decisions when entries are removed. |
| `004b941` | Replace reviewed source authorships without double counting. |
| `8d3b737` | Prevent heuristic preprint links bridging archival works. |
| `38a7eae` | Validate affiliation evidence against person-owned versions. |
| `071c083` | Refresh offline affiliation evidence when DOI cache changes. |
| `2bc7a44` | Publish immutable assets before manifests and retain prior readers. |
| `20b8299` | Refresh audited identities, publication evidence and production exports. |

This report and selected small evidence/probe files are committed separately from those fixes and production exports. No HTML/CSS/frontend-design source was changed. Ignored backups, source downloads and staged/full experimental exports are not committed.

## Follow-up: affiliation ownership and the flagged historical tutorials

Implemented after the audit commits above, on the same branch:

- `scripts/refresh_affiliations.py` preserves `person_version_dblp_keys` from the canonical CSV. `scripts/automatic_affiliations.py` uses only same-year owned versions for DOI lookup, permits only the selected record for legacy rows, and permits none for explicit empty ownership. Applying automatic evidence checks the source DOI against owned versions again. Policy version **2 → 3** invalidates old snapshots automatically.
- Regression fixtures first reproduced three failures and two missing-ownership errors on the original code. New tests cover the real CSV-to-evidence path, plausible matching metadata on an unowned version, runtime application, retained owned DOI evidence, legacy/empty ownership and old-policy snapshot migration. The final combined Python suite passes **368 tests**, zero failures/errors/skips.
- Rebuilt automatic evidence offline using the same identity/publication bytes and cached metadata: zero requests. Helen Huang's one unsupported ADC DOI lookup disappears; no accepted candidate is removed. All 775 people with accepted publisher evidence remain. Repeat offline execution is byte-identical.
- Reviewed all **13 entries in the SIGMOD 2017 Tutorials section** of the [proceedings](https://dblp.org/db/conf/sigmod/sigmod2017.html), including the author-confirmed [Kumar/Boehm/Yang tutorial](https://research.ibm.com/publications/data-management-in-machine-learning-challenges-techniques-and-systems). Exact-key exclusions now persist in the ordinary pipeline. Seven six-page tutorials had contributed **14 researcher–work pairs**; the other six already contributed zero. Fourteen people each lose one DB evidence paper, with no label changes. The papers remain browseable.
- Rebuilt and promoted website/coverage exports with normal freshness checks and assets first. All 1,191 references exist; all 125 inspection descriptors and 36 nodes/37 ribbons reconcile. DB research evidence changes from 10,290 to 10,283 unique core works, with the seven tutorials retained as context. Main-conference membership, all displayed affiliations and Home summary bytes are unchanged: **833 people, 227 main authors, 533 works, 648 authorships**.

Existing Chrome checks pass **182 presentation + 90 Home links + 782 Sankey checks**. Follow-up evidence is in `data/reports/round2_followup/` locally; staged datasets and before copies remain ignored.

This closes the demonstrated automatic-affiliation bug and the specifically flagged 2017 tutorial gap. It does not assert that every historical proceedings track has been externally reviewed. Jia Yu's source biography still spans the observed WSU/Wherobots roles, but the competing DBLP profiles leave publication ownership unresolved; no PID was forced. The prior ORCID flags are conflicting source evidence, including corroborated correct ownership, not established errors that can safely be reassigned wholesale. Those uncertainty flags remain; no new identity or authorship override was introduced in this follow-up.


## Follow-up: supported identity corrections, source deduplication and four nonresearch works

Implemented on 7 November 2026 after the read-only correctness recheck. The DBLP snapshot remains `dblp-2026-10-03` and the ROR release remains `v2.14-2026-10-06`. Classification thresholds and website design are unchanged.

**Identity corrections.** Added three scoped, dated reviews to `data/reviewed_person_identities.csv`, supported by official committee affiliations, institutional biographies, author CVs and matching named publications. These are evidence-backed decisions, not a rule choosing whichever profile contains more papers. The normal pipeline reapplies them while the candidate, assignment and service-affiliation guards remain valid.

| Researcher | Corrected DBLP PID | DB papers | Main ML papers | Result |
| --- | --- | ---: | ---: | --- |
| Brian F. Cooper | `12/875` | 10 | 0 | DB-focused |
| Yisu Remy Wang | `185/9964` | 5 | 0 | DB-focused |
| Djellel Eddine Difallah | `09/10814` | 2 | 0 | Limited evidence |

Brian Cooper's four confirmed short-profile works and Djellel Difallah's five records are preserved with exact-key, exact-signature entries in `data/reviewed_publication_authors.csv`. These are nine author assignments, not nine additional distinct works: publication versions are still deduplicated normally. Yisu Wang's unrelated short-profile bibliography is not transferred. No whole-profile publication merge or global short-name alias was added.

**Source deduplication.** ICDE and EDBT parsers and source merges retain different normalized affiliations for the same year, role and name. Repeated identical observations still collapse; ICDE's blank-affiliation enrichment and dedicated reviewer-source ownership are preserved. Replayed all 24 already-used official source URLs for 2022–2025. Fourteen responses match previously recorded bytes; the other ten have different HTML bytes. Both parser versions were compared on the same newly saved responses. The fixed results preserve every stored year/role/name/affiliation observation and add 11: seven ICDE and four EDBT. Ten are alternate chair labels; one is the second ICDE 2025 Lu Chen institution. Lu Chen remains ambiguous rather than being forced to a PID. No new name group or resolved person was inferred from these additions.

**Nonresearch evidence.** Added the four reviewed exact-key exclusions: `conf/sigmod/ChuIKW16`, `conf/sigmod/OzcanQSLE20`, `conf/sigmod/MiloS20` and `conf/cidr/SinhaTMW07`. Their official/author-hosted tutorial or demonstration evidence is recorded in `data/focus_publication_exclusions.csv`. The fixed-input production comparison removes nine researcher–work classification contributions across nine people (eight DB-focused members and one Limited-evidence member); none changes label. The publications remain available as context. The reviewed exact-key exclusion list now contains 33 entries, including the preceding follow-up's 2017 tutorials.

**Rebuild and measured impact.** Backed up the current source/identity databases and generated inputs before adoption, then replayed all guarded identity stages. Exactly the three intended PIDs changed. Rescanned all 12,998,506 records in the pinned DBLP XML, rebuilt work entities using the existing arXiv cache and refreshed publisher affiliation evidence offline. No supporting-source API requests were made; all 775 researchers with accepted publisher evidence remain represented. Djellel Difallah's evidence follows the corrected PID from 409/8145 to 09/10814; evidence on all unchanged PIDs is identical. Normal freshness guards were used.

- Service observations: **4,669 → 4,680**; name groups **1,450**, resolved identities **1,125** and qualifying PC candidates **1,114** are unchanged.
- DB-focused population: **833 → 835**. Limited evidence: **224 → 222**. ML-focused **27** and Mixed **30** are unchanged. The two newly included members meet the existing publication minimum; Djellel Difallah remains Limited evidence.
- Main ML membership and publications are unchanged: **227 authors, 533 distinct works, 648 researcher–work pairs**. The complete existing pair set is identical, as are both Home trend series and the conference-wide total CSV bytes. Population denominators and venue membership counts update from the data.
- Raw researcher–record links: **129,362 → 129,478**; canonical researcher–work links **117,442 → 117,552**; distinct canonical works across all resolved identities **88,166 → 88,228**. The raw results of all **1,122** unaffected profiles are identical.

Rebuilt and promoted the Home, browser, matrix and coverage exports, publishing immutable assets before the manifests and retaining existing assets for open readers. All **1,191** referenced assets exist and their content identifiers were checked using the generator's newline conventions. All inspection descriptors and Sankey components reconcile with the production inputs.

**Validation.** The complete Python suite passes **376 tests**. New source fixtures reproduced the name-only data loss before the fix and now cover distinct affiliations, true duplicates, years, roles and blank-label enrichment. Reviewed-identity tests reject changed affiliations, missing candidates and changed assignments; author-review tests prevent transfer of the Yisu namesake's papers. Existing Chrome integration checks pass **182 presentation + 90 Home links + 782 Sankey checks** against the promoted files. The independent export reconciliation reports zero failures.

Evidence and reproducible local helpers are under `data/reports/correctness_fixes/`: `source_replay_evidence.json`, `icde_source_replay.json`, `edbt_source_replay.json`, `identity_stage_result.json`, `raw_scan_verification.json`, `affiliation_verification.json`, `verification.json`, `promotion.json`, `exports_evidence.json` and `browser_checks.json`. The `before/` copies, downloaded pages, databases and full staged exports remain ignored local artifacts. Reviewed decision CSVs and the regression tests are persistent pipeline inputs/code.

The first browser attempt hit the already-flagged resize assertion. A diagnostic run with the same assertion and wait interval passed, recording the same document/window and a chart viewBox changing from 1,240 to 351 and back to 1,240 as its container resized. The unmodified suite then passed. The initial failure is preserved in `browser_checks_attempt1.json` and the diagnostic trace in `resize_diagnostic_dom.html`. This establishes an intermittent check result, not its cause; no frontend change was made to hide it.

This closes the three supported findings from `correctness_recheck/findings.txt`. It does not establish that every historical nonresearch track or unresolved identity has been exhaustively verified. Jia Yu and other genuinely ambiguous cases remain flagged; no unsupported reassignment or threshold relaxation was introduced. No commit or push was made for this follow-up.


## Follow-up: complete technical committees, reviewed identities and reliable refreshes

The correctness pass preserves the pinned DBLP monthly snapshot (`dblp-2026-10-03`), ROR release (`v2.14-2026-10-06`), classification thresholds and website design. It changes source coverage and reviewed evidence, so the resulting counts change.

### Source coverage

Replayed all 56 previously used SIGMOD/PODS, VLDB, ICDE, EDBT and CIDR URLs across 2022–2025. The official annual PVLDB front matter also publishes industry and demonstration committees absent from the HTML-only collection. The normal VLDB scraper now fetches and hashes these PDFs, validates volume and explicit track headings, preserves wrapped affiliations and records the printed PDF page. Tutorial committees in these added PDFs are not collected. Demonstration associate editors are qualifying technical-track senior reviewers, rather than research-track editors.

The four PDFs contribute **419** additional service observations: 108 in 2022, 99 in 2023, 101 in 2024 and 111 in 2025. All 419 extracted names were independently checked against text from a second PDF extractor on their recorded pages. Selected pages were also visually inspected. The 2022 printed demo roster contains 73 names; the parser follows the named roster rather than replacing it with an editorial summary count of 72.

Primary sources: [2022, volume 15](https://www.vldb.org/pvldb/vol15/FrontMatterVol15No12.pdf), [2023, volume 16](https://www.vldb.org/pvldb/vol16/FrontMatterVol16No12.pdf), [2024, volume 17](https://www.vldb.org/pvldb/vol17/FrontMatterVol17No12.pdf) and [2025, volume 18](https://www.vldb.org/pvldb/vol18/FrontMatterVol18No12.pdf).

Eight official EDBT track call pages were checked separately. Five contain member rosters. The 2022 industry/demo calls and 2023 industry call do not publish member rosters; that absence is now explicit rather than treated as a failed required download. Reconciliation adds eight EDBT observations and seven SIGMOD/PODS alternate affiliation observations. **Every one of the prior 4,680 year/role/name/affiliation observations remains; 434 are added, for 5,114.** These are observations, not distinct people.

### Identity and publication evidence

Added eight scoped decisions across seven researchers: directly corroborated identity reviews for Angelos Christos Anadiotis, Yuanyuan Tian, Allison Lee and Tiago Eugenio de Melo, plus roster-alias reviews for HV Jagadish, Joe Hellerstein, Tamer Özsu and the Yuanyan Tian spelling. Four existing review scopes were extended to cover independently supported affiliation labels recovered from the source replay. The dated institutional, author and conference evidence lives in `reviewed_person_identities.csv` and `reviewed_service_aliases.csv`.

The alias table can correct an explicitly named previous automatically accepted exact-name PID. It still requires a stable resolved anchor, matching affiliations and an existing target profile. It cannot replace an unrelated or independently evidenced assignment. Changed scope or missing evidence holds the case for review. An unresolved identity review may leave `previous_pid` empty. Two conflicting ORCIDs on the Anadiotis profile are not arbitrarily reduced to one verified identifier.

Three exact-key author reviews preserve POLAR (Allison Lee), the PVLDB front-matter typo record (Yuanyuan Tian) and the SIoT full-name record (Tiago de Melo). They preserve individually observed split records without blanket profile merges or global publication aliases. Front matter and SIoT remain context rather than selected DB-focus evidence.

All **1,125** previously accepted PID assignments remain stable, and their publication keys, ownership and matching evidence remain unchanged. One generated canonical-name capitalization changes from MingJie Tang to Mingjie Tang; the other 1,124 raw profiles are identical. Name groups increase from 1,450 to 1,534; 1,240 groups resolve to **1,187** distinct PIDs. Of those, **1,178** have qualifying PC service. Different source spellings are not counted as additional people.

### Research-paper evidence

Added **92** dated exact-key tutorial decisions from explicitly labelled SIGMOD proceedings sections, the official [PODS tutorial index](https://sigmod.org/pods-home/pods-tutorials/) and official SIGMOD/ICDE tutorial lists. There are now **125** reviewed exact-key nonresearch exclusions. The publications remain available as context.

On the unchanged previous population, these corrections remove **48 researcher–publication contributions across 31 distinct records and 44 people**. They change no existing classification label by themselves. Short tutorials already excluded by the length rule are also recorded explicitly. All 105 reviewed keys observed in the final focus audit are excluded and retain their evidence URLs; the rules fingerprint matches the final files.

Suspicious wording alone is insufficient evidence. For example, the original [ICDE 2024 table of contents](https://www.proceedings.com/content/075/075189webtoc.pdf) places the large-language-model applications paper in **Data Engineering Future Technologies Papers**, outside its Tutorials section. It is not excluded merely because it resembles a tutorial. The same principle preserves independently confirmed industrial research papers with “demonstration” in their titles.

### Rebuild and measured results

Source and identity changes were replayed privately with pre-change database and generated-input backups. The complete 12,998,506-record DBLP dump was rescanned, work entities were rebuilt and cached publisher affiliations were refreshed offline. Conference-wide paper totals are byte-for-byte unchanged. All 775 previous researchers with publisher affiliation evidence retain that evidence; 11 more have cached evidence, for 786. There were zero supporting-source API requests. Three evidence display names change with newly recognized roster spellings; the evidence itself is unchanged on every existing PID.

| Measure | Before | After |
|---|---:|---:|
| Qualifying, resolved PC members | 1,114 | 1,178 |
| DB-focused members | 835 | 868 |
| ML-focused members | 27 | 27 |
| Mixed members | 30 | 33 |
| Limited evidence | 222 | 250 |
| DB-focused members with a main ML paper | 227 | 232 |
| Distinct main ML works in the study | 533 | 538 |
| Main ML researcher–work pairs | 648 | 659 |
| Raw researcher–publication links | 129,478 | 132,723 |
| Canonical researcher–work links | 117,552 | 120,523 |
| Distinct works across resolved identities | 88,228 | 90,098 |

The 64 newly eligible PC candidates comprise 32 DB-focused, three Mixed and 29 Limited-evidence members. One existing candidate, Björn Þór Jónsson, additionally qualifies through newly observed PC service in a second year: his four DB papers and zero ML papers are unchanged. This explains the increase of 33 DB-focused members. The final population has 66 repeated-service fallback qualifiers. All 648 previous main-ML researcher–work pairs are retained.

### Refresh and rendering safeguards

All five venue scrapers return a nonzero status if a required year/source fails, stopping the normal pipeline before it can report a complete refresh or publish derived data. Required technical-track download/parser failures no longer silently skip that source. Existing yearly records remain intact. EDBT also rolls back partial source metadata so a later successful year cannot commit metadata from an earlier failed year.

Chart containers are observed for actual width changes, including changes without a window resize. Width guards prevent height-only redraws. Redraws use a queued task outside the observer's layout pass, avoiding dependence on a suspended animation-frame callback. The browser test waits on the tested iframe's task queue and includes a container-only change with **zero window resize events**. Earlier failing attempts and the diagnostic trace are preserved; the size assertions were not weakened.

### Validation and remaining limits

The final Python suite passes **387 tests**. Real Chrome checks pass **183 presentation + 90 Home-link + 782 Sankey checks**. Independent reconciliation passes both before and after promotion: all **125** inspection descriptors, **36** Sankey nodes, **37** links, record subsets, units, ownership and publication-search/detail counts reconcile. All **1,255** referenced assets exist and their content identifiers were checked. Published manifests match the checked staged bytes. Immutable assets were published first and older published assets remain available to open readers.

This does not establish exhaustive coverage of every historical source or paper type. **232 ambiguous name groups, 32 candidate groups and 30 unmatched groups remain held**, with inspection evidence available. A name group is not necessarily a distinct person. In particular, a source name shared by researchers at different institutions cannot safely be forced onto one PID. Those cases need observation-level primary evidence or a reviewed split, rather than a larger-profile or loose-name heuristic. Historical nonresearch-track review is bounded to the sources checked here; future snapshot/source changes still require the guarded reviews to remain valid.

Reproducible local evidence is under `data/reports/correctness_round3/`: `source_recall_audit.json`, `edbt_track_availability.json`, `pdf_independent_validation.json`, `source_changes.json`, `identity_changes.json`, `split_publication_records.json`, `tutorial_counterfactual.json`, `exclusion_verification.json`, `affiliation_verification.json`, `verification.json`, `promotion.json`, `exports_evidence_staged.json`, `exports_evidence.json`, `python_tests.json`, `browser_checks.json` and the resize failure/diagnostic files. The reviewed CSVs and tests are persistent pipeline inputs/code. To recover disk space, 2,067,763,941 bytes of older ignored staging files were removed only after confirming they were byte-identical to published files; source data, baseline backups, reports and published assets were preserved. No commit or push was made.


## Follow-up: November DBLP namesake stubs and persistent identity reviews

The full online refresh selected **DBLP `dblp-2026-11-01`** and **ROR `v2.15-2026-10-20`** and passed all 387 then-existing Python tests. Its 5,114 service observations exactly match the previous audited build. The DB-focused count changed from 868 to 862; five previously accepted service identities became unresolved.

**Root cause.** Independently rescanned all **13,073,341** top-level November XML records and saved the ten relevant original person records. Each of the five new exact-name competitors contains only an author name and a Home Page title, without an affiliation, homepage URL or ORCID. They were created on 6–26 October and were absent from the 3 October dump. Their original, institution-backed counterpart records are unchanged. These new records are not marked as disambiguation pages; empty metadata alone is not grounds to discard them as nonpeople or to merge them into another profile.

The existing complete-competitor checks (`apply_high_precision_entity_resolution.py:88`, `apply_ror_entity_resolution.py:145`) correctly stop automatic affiliation matching when an exact-name competitor lacks the required evidence. This was a recall loss caused by new incomplete source evidence, not a demonstrated swap to a wrong PID or a lost committee roster. Automatically retaining every historical match would bypass that safeguard.

**Evidence-backed recovery.** Added five dated, exact-PID reviews to `data/reviewed_person_identities.csv`. All observed service affiliation labels are explicitly covered. The normal existing reviewed-identity stage applies these only while the target remains a real candidate and the service affiliation and accepted-assignment guards remain valid. No general matching rule, classification threshold, publication alias or whole-profile merge was added.

| Service name | Reviewed PID | Independent primary evidence |
|---|---|---|
| Dong Deng | `71/8700-1` | Rutgers faculty and institutional research outputs |
| Eugene Wu | `41/3679` | Columbia homepage and author-specific ACM record |
| Lihong He | `35/850` | IBM biography, IBM publication record and official SIGMOD demo listing |
| Qingqing Ye | `194/0001-1` | PolyU biography with explicit ORCID and personal-homepage links |
| Yufei Tao | `t/YufeiTao` | CUHK directory with explicit ORCID and PODS24 committee service |

The review rows link the actual institutional/publisher sources. Bibliography-level ORCID warnings on the live Eugene Wu and Lihong He pages were not treated as evidence that all works belong to another person or as unique-ORCID proof. Their identities are independently anchored by institutional history, linked homepages and exact author-specific publication evidence. No publication-level ORCID was substituted or used to reassign papers.

**Verified rebuild.** Ran the ordinary pipeline with `--offline` against the newly refreshed November sources, without freshness overrides. The recovered profiles exposed eight previously uncached arXiv IDs. Then ran the pipeline's normal publication-enrichment, affiliation, export and validation stages online to refresh their supporting evidence without redownloading the snapshots or repeating source/identity collection. That auxiliary export command inherited the pipeline's timestamp-reuse option; an independent `assert_pipeline_freshness(..., allow_stale=False)` check validates all final upstream inputs strictly rather than relying on that bypass. Exactly the five intended assignments changed; every pre-existing accepted assignment, raw publication key/ownership/evidence field and existing classification decision in the November pre-fix build is preserved. Conference-wide NeurIPS/ICML/ICLR denominators are unchanged. All 5,114 source observations remain. No publications were transferred from the new unqualified stub profiles.

The initial byte-for-byte raw-profile assertion also flagged the already-observed display-label variation for PID `01/7213`: **MingJie Tang → Mingjie Tang**, in 83 rows. Independent field-level inspection found no added/removed paper keys or other changed columns. The final reconciliation permits only case-fold-equivalent `canonical_name` changes and records the exception explicitly; all other raw-profile fields remain strict. This is a case-only label variation, not a person reassignment.

- Resolved people: **1,184 → 1,189**; qualifying PC people: **1,175 → 1,180**.
- DB-focused people: **862 → 866**. Lihong He returns to the broader PC population as **Limited evidence**, not DB-focused.
- DB-focused main-ML authors: **232 → 235**; distinct main-ML works: **559 → 564**; researcher–main-work pairs: **670 → 677**.
- Focus groups: `{"DB-focused": 866, "Limited evidence": 250, "ML-focused": 28, "Mixed": 36}`.

The difference from the preceding October build's 868 DB-focused people is not undone artificially: November's additional ML papers move Zeyi Wen, Ritesh Sarkhel, Chenjuan Guo and Shuigeng Zhou into Mixed under the unchanged two-to-one rule, while Qichen Wang and Zhengyi Yang newly resolve and qualify. These four classification changes and two new qualifying identities remain intact.

**Validation.** The new regression reproduces all five missing persistent reviews before the data fix. It now verifies acceptance of the documented targets despite unaffiliated competitors and rejection of unreviewed affiliations, missing targets and conflicting accepted assignments. All **388 Python tests** pass. Real Chrome passes **183 presentation + 90 Home-link + 782 Sankey checks**. Independent reconciliation passes all **125** coverage descriptors and all person/paper flow components, detail ownership, search/detail counts and content identifiers.

A first local logging-helper attempt failed on Windows cp1252 output for a Unicode researcher name, before any website rebuild. Its log is retained; the helper was corrected to use UTF-8, the prior pipeline process was confirmed exited and the successful run restarted through the normal pipeline. No production code was changed to conceal that failure.

Evidence is retained locally under `data/reports/full_pipeline_2026-11-09/`: `comparison.json`, `candidate_changes.json`, `identity_snapshot_records.json`, `before_namesake_fix/`, `namesake_rebuild.json`, `namesake_rebuild.log`, `namesake_support_refresh.json`, `namesake_support_refresh.log`, `namesake_fix_verification.json`, `namesake_fix_exports.json` and `namesake_fix_browser_checks.json`. Reviewed CSV decisions and the regression test are persistent repository changes. Website design is unchanged. No commit or push was made.


## Follow-up: persistent contextual-paper category reviews (9 November 2026)

Three independent batches inspected the 179 flagged works (181 DBLP record rows). Cross-review challenged generic conference citations in mixed volumes; ambiguous tracks were retained as pending instead of being certified. The tracked `data/publication_category_reviews.csv` contains all 181 records, expected bibliographic metadata, proposed category, confirmation/pending status, dated rationale, source URL and verification method. There are 137 live-primary-source evidence rows and 44 pinned-snapshot-only metadata rows. These are source inspections, not a claim that a human read every full paper.

175 works (177 records) have supported assignments. Four records remain pending, with their precise missing-evidence questions in the ledger:

- `conf/acii/DollackLYYSK22`: workshop contribution or conference demo.
- `conf/green/PathakTW11`: IGCC main conference or affiliated workshop.
- `conf/securecomm/Kerschbaum07`: exact SecureComm2007 main/workshop track.
- `conf/wlp/ShangSG04`: INAP conference or WLP workshop within joint proceedings.

175 exact-key, year-bounded rows were appended to the existing tracked category mapping CSV. The two reviewed preprint records retain protected preprint recognition. Mixed volumes were not mapped wholesale: DASFAA2019 Posters/Demos stay Other conference; DCOSS/ANT workshop tracks change individually; conference/preprint versions retain their own categories. Of the inspected records, 112 were assigned Workshop, 3 Other (two challenge contributions and an edited-book chapter), 64 retain Other conference (including four pending) and 2 remain Preprint. This changes 115 record-level proposed labels.

The audit now revalidates persistent reviews before every normal pipeline website build. Confirmed metadata/category conflicts stop the run before export; pending cases remain visible without certifying a fallback. Reviewed version-specific categories no longer reappear as unexplained conflicts. Audit summaries use repository-relative file references. A relocated checkout (including a path with spaces and an unrelated working directory) loads all reviews and mappings and completes the pipeline dry run. This is a path/rebuild-plan check, not another multi-gigabyte source refresh.

After rebuilding, all publication membership, authorship, focus decisions, conference-wide denominators and search titles are unchanged. Core counts remain 866 DB-focused people, 235 with main ML papers and 564 unique main ML works. The core contextual Workshop count changes 5,940 ->6,008; Other conference 24,222 ->24,152; Other 1,553 ->1,555. Across the full eligible Explorer, 114 changed record keys appear in 121 researcher-record pairs. One reviewed changed key is outside the eligible Explorer population; the full category audit records all 115 changed labels.

Validation: all 396 Python tests pass; 125 coverage descriptors and all Sankey components reconcile; strict pipeline freshness passes; 1,055 real-clock Chrome checks pass (183 presentation, 90 Home-link, 782 Sankey). The earlier simulated-time browser harness timed out during a container resize. The same assertions pass with normal browser time; the portable checked-in runner `tests/run_browser_checks.py` avoids that scheduling artifact. No product styling or frontend behavior was changed in this follow-up.

Backups are under ignored `data/backups/`; no backup is tracked. Research scratch/evidence for this follow-up stays under ignored `data/reports/publication_review_2026-11-09/`. Operational decisions and sources are in the tracked CSVs, with loader/audit code, regression tests and rebuild documentation available to commit. Ignored working data can be regenerated online from the pinned dumps and source services. Live metadata/pages can change, so byte-for-byte historical reproduction additionally requires retained source caches/backups. No hardcoded local runtime paths were found. No commit or push was made.


## Follow-up: researcher and publication Sankey inspection (9 November 2026)

Three specialists reviewed people-flow evidence, paper-flow scope and rendered usability, then cross-reviewed the proposed changes before implementation. The main findings were lost candidate metadata on exact researcher ribbons and inconsistent units/populations in the paper review shortcuts. No matching decisions, classification thresholds or pipeline counts were changed.

- Researcher ribbons now retain the same candidate comparisons, affiliation evidence and CSV context as the target name groups. All 212 several-candidate-to-ambiguous rows retain their full stored comparison data; the seven one-candidate and eleven zero-usable-candidate rows retain their respective review evidence. Zero-candidate routes explicitly explain that no usable person candidate is recorded. Accepted sparse-evidence flags remain review flags, not proven mismatches.
- Known structured identity-review notes are presented as a readable reason, review date and supporting URLs. Original notes remain available in the existing optional disclosure. Identity/publication review URLs are labeled separately from committee-page sources and survive search and CSV export. Unknown note formats are preserved verbatim.
- The 451-work unverified-DB node now links an exact cohort/work-level inspection, with paired DBLP record, pages and uncertainty reason. The broader 487-record queue remains a separate quality diagnostic. The five possible additional PC members are explicitly a broader what-if scenario, not recoverable identities or automatic additions.
- Four pending display-category questions remain distinct from DB-focus evidence: three cohort context works and one outside-cohort work have separate related inspection groups. Confirmed/pending review dates, sources and version keys also appear in paper inspection evidence.
- Nodes show short counting units. Percentage labels use one decimal and positive shares below 0.1% use `<0.1%`. Ribbon tooltips identify outgoing/incoming node-flow denominators and explain merge accounting where units change. Compact charts have Previous/Next controls. Correct proportional widths, full-stage desktop layout and independent measurement switches remain.

All 23 researcher nodes/25 ribbons and 13 paper nodes/12 ribbons retain exactly their saved counts. Core totals remain 866 DB-focused researchers, 235 main-ML authors and 564 unique main-ML works. Full export reconciliation passes for all 129 descriptors, including hashes, ownership, precise subsets, totals and conservation. Strict pipeline freshness passes. All 402 Python tests and 1,318 real-clock Chrome assertions pass (183 presentation, 90 Home links, 1,045 Sankey checks). A first browser run caught inconsistent percentage formatting in the identity overview; this was corrected before the successful full run.

Rendered geometry was checked at 320, 390, 800, 950, 1000, 1100 and 1440 px with no horizontal overflow or clipped visible labels; desktop researcher and mobile paper screenshots were also inspected. Chart text and controls remain concise. The diagnostic screenshot helper produced its outputs but reported a Windows temporary-profile cleanup lock; this did not affect the subsequent successful portable browser suite.

The website dataset and matrix were rebuilt through their normal builders. Changed source, tests, docs and generated browser assets are available to commit. Baseline outputs, screenshots and local verification logs stay in ignored `data/backups/sankey_review_2026-11-09/` and `data/reports/sankey_review_2026-11-09/`. No commit or push was made.

## Follow-up: ambiguous service identities (reviewed 9 November, completed 13 November 2026)

Of the 230 ambiguous name groups, 47 have independent evidence sufficient to identify a DBLP person. Exact-name ambiguities fall from 221 to 175 and variant ambiguities from 9 to 8: 183 remain open. Distinct matched people increase from 1,189 to 1,236. Existing accepted identities and all 5,114 service observations are preserved.

The decisions are stored in the tracked `data/reviewed_person_identities.csv`, with the target PID, covered service affiliations, primary evidence URLs, review date and reason. Institutional homepages, person-level ORCIDs and author-specific publisher evidence establish the identities. The normal review tool revalidates candidate membership and affiliation scope on future runs. Publication counts and an earlier automatic acceptance are not identity evidence. No PC-service threshold or DB-focus classification rule was changed.

Examples include George Fletcher (TU/e's explicit ORCID), Ke Yi (the HKUST homepage linked by the pinned DBLP profile) and Jin Wang (ASU's explicit DBLP and ORCID links with the earlier Megagon appointment). Xin Wang requires selecting a different candidate from the highest affiliation score: the [Tianjin database group homepage](https://www.tjudb.cn/dbgroup/Xin_Wang) publishes ORCID `0000-0001-9651-0651`, identifying PID `10/5630-30`. PID `10/5630-133` instead identifies a Fujitsu researcher whose former Tianjin affiliation was a PhD in 2005. The university biography gives the database professor's Nankai PhD in 2009. Conflicting ORCIDs elsewhere in a live bibliography do not establish ownership of every publication or authorize merging profiles.

Seven of the 54 prioritized affiliation cases remain unresolved:

| Service name | Evidence still needed |
|---|---|
| Hui Li | More than one candidate has Xidian affiliations; the institution alone does not select the committee member. |
| Ning Wang | Two candidates have Beijing Jiaotong affiliations; a direct committee-to-person identifier is needed. |
| Min Xie | Instacart and Shenzhen Institute affiliations appear to describe different researchers, with different PhDs. The [Shenzhen researcher's biography](https://mxieaa.github.io/) gives HKUST; the Instacart candidate's pinned profile gives UBC. The whole name group must not be assigned to either one. |
| Jia Yu | The [personal homepage](https://jiayuasu.github.io/) stored on PID `86/3457-1` accounts for WSU and Wherobots, while PID `86/3457-20` separately names WSU/Apache. A reviewed plan for the split publication profiles is needed before treating the group as fully resolved. |
| Mohamed S. Hassan | Purdue sources support the database PhD and Oracle, but the Google/Oracle career and name variants need stronger corroboration. |
| Peng Han | An electrical-engineering namesake also had an Aalborg visit. The committee member needs more specific evidence. |
| Wei Huang | The pinned Fuzhou homepage returns 404 and other researchers of this name also work at Fuzhou. A working primary identity link is needed. |

The other remaining groups retain their candidate evidence in the Sankey inspection and unresolved-name audit. Their presence in that queue does not establish that the people are absent from DBLP.

**Final rebuild and checks.** All 866 previous study members remain included. Of the 47 recovered people, 41 qualify as DB-focused, three have Limited evidence, two are Mixed and one is ML-focused. The study therefore contains 907 DB-focused people, including 256 with main ML papers (previously 235), and 598 distinct main ML works (previously 564). The broader explorer contains 1,227 matched PC members. The other nine resolved people remain outside the qualifying PC population.

The first 46 decisions passed a full offline pipeline rebuild against the pinned November snapshot. The final Xin Wang decision was applied with the normal guarded review tool against the unchanged candidate index, followed by another full publication scan and the normal enrichment, affiliation, category audit, website and matrix stages. The supporting refresh fetched 294 missing arXiv records, enabling 24 further version merges; the final unresolved-preprint queue has no uncached arXiv IDs. Publisher refreshes reported no failed requests. Identity source/rule invariants were checked before recording the completed pipeline fingerprint; strict freshness passes without a stale-input override.

The before/after comparison preserves all 5,114 service observations, all previous accepted identities and all 133,672 previously supported researcher-record links. It adds 7,384 researcher-record links. Full export reconciliation passes for all 129 coverage descriptors, including exact subsets, ownership, hashes, counting units and flow conservation. PC-service and DB-focus policy inputs remain byte-identical.

All 402 Python tests and 1,322 real-browser assertions pass (187 presentation, 90 Home links and 1,045 Sankey checks). The browser check initially exposed its own one-page assumption: the default explorer now has 256 matches, exceeding the selected 250 rows per page. The test now verifies every page, exact page lengths, the final Next-button state and unique complete researcher IDs; explorer behavior was unchanged.

Decisions and source URLs are retained in the tracked review CSV. The audit summary and pagination regression check are also tracked. Diagnostic evidence remains in ignored `data/reports/ambiguity_and_pc_fallback_2026-11-09/` and the original backup in ignored `data/backups/ambiguous_identity_review_2026-11-09/`. Those diagnostics are not runtime dependencies: the ordinary pipeline can reproduce the decisions from the tracked CSV and pinned sources. No commit or push was made for this completion.

</details>
