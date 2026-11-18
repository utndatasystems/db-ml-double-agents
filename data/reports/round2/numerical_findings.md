# Specialist 7: independent calculations and temporal meaning

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../../docs/PIPELINE.md) for current behavior.

Initial pass was read-only except for these small files under `data/reports/round2`. Baseline: branch `audit/backend-correctness-2026-11-06`, HEAD `567b35b`, pinned DBLP `dblp-2026-10-03`; no new sources, production mutations or full scan. Shared frontend/build files remain coordinator-owned. Commands below use the existing local inputs.

## Independent current-data reconciliation

`numerical_probe.py` reads the canonical publication CSV directly, restricts to the 833 exported included PIDs, and counts PID/work pairs, work IDs, venues, years and first appearances independently of the Home summary builders. Result: **833 people, 227 main-conference authors, 533 distinct main works, 648 researcher/work pairs**. Main works: NeurIPS 247, ICML 135, ICLR 151. All **75** exported Home annual points match CSV work-set counts and venue denominators exactly; all ten 2016–2025 first-time-author counts match each PID's earliest recorded main publication across the full history.

Representative 2025 annual shares are NeurIPS **84/5,823 = 1.442555%**, ICML **45/3,268 = 1.376989%**, ICLR **47/3,704 = 1.268898%**. Five-year pooled 2021–2025 shares are **213/19,025 = 1.119580%**, **107/10,123 = 1.056999%**, and **95/9,491 = 1.000948%**. These are ratios of sums, rather than unweighted averages of annual percentages, matching `website/assets/home-trends.js:4` and Home's description at `website/assets/app.js:1282`.

Current display window begins 2006 (2026 minus 20), with shared coverage ending 2025. Independently calculated CAGR: NeurIPS **5.091040%** (2010–2025), ICML **3.514951%** (2006–2025), ICLR **5.149307%** (2018–2025). Runtime `publicationWindow` at `home-trends.js:26` intentionally selects the first available positive share within that window; the historical backend `growthComparison` may have an earlier baseline and is not itself the displayed CAGR. This difference is documented by `app.js:1283`, not a demonstrated bug.

The first-time annual counts are **1, 2, 6, 15, 12, 18, 23, 28, 44, 52**. Independent Python least-squares fitting, using a finer exponent grid and golden-section refinement instead of JavaScript's coarse-grid/ternary refinement, gives **33.0888875% annual growth**, SSE **74.7717285** and initial amplitude **4.1027342**. The stored `annualizedGrowth` endpoint CAGR is **55.1197293%**. Home intentionally uses the fitted growth (`website/assets/home.js:290`), not the stored endpoint field; Methodology explicitly distinguishes these at `app.js:1288`. The mismatch is not a wrong displayed calculation.

Expected cumulative Relative values through 2025, including venue totals before the cohort's first paper, are NeurIPS **247/30,610 = 0.806926%**, ICML **135/16,483 = 0.819026%**, ICLR **105/11,214 = 0.936330%**. Implementation at `app.js:1988` accumulates numerator only for covered years and denominator from the earliest available venue total, shows unavailable at an uncovered selected year, and retains all publications in Absolute mode. Methodology at `app.js:1276` describes that behavior. I inspected the code and independently computed the expected current values; this pass did not run a new browser interaction for every cumulative point.

## Missing totals, zeroes and current/future years

The existing Home population boundary suite was rerun, rather than treating the prior report's passing status as proof. It exercises coauthor/work deduplication, missing denominators, non-shared endpoint coverage, zero endpoints and baselines, a single-year comparison, an excluded profile, first appearances outside the display window, duplicate people/venues and future-only authors. Runtime smoothing rejects missing/nonpositive denominators and calendar gaps; CAGR rejects zero starting values but permits a zero ending value (`home-trends.js:4,21`). The runtime publication window excludes years later than the supplied current year (`:26`). No change to these rules is justified by this pass.

Actual 2026 main records: **56 author/work rows, 46 works, 43 people, all ICLR; eight people first appear in 2026**. There are no exported main records after 2026. Home intentionally ends both trend panels in 2025, the latest shared denominator year, while headline lifetime author counts include 2026. The headline publication-year range is explicitly 2001–2026. All Insights can show the partial venue coverage and emits `2026 coverage: ICLR only.` through `app.js:727`; Methodology states that refresh dates do not establish completeness (`app.js:1362`). A positive venue total does not establish that the venue/year is complete. No annual-completeness flag was independently validated, and no threshold or endpoint policy was changed.

These are retrospective histories of the fixed currently eligible 2022–2025 service cohort, not reconstructed historical DB-focused populations. Labels explicitly say “among DB-focused PC members”; this is mathematically consistent, but interpreting the figures as causal migration or a year-specific eligible population is unsupported. Adding historical cohorts would be a methodological change, not a bug fix.

## Candidate issue investigated but not established as a current numerical bug

Frontend `paperKey` (`app.js:1235`) deduplicates by the person's selected DBLP key, while Home's work sets use work IDs (`scripts/home_population.py:57`). A merged work could theoretically expose different owned selected records to different people. Actual included main data contains **533 work IDs and 533 selected keys**, and **zero work IDs with multiple selected main keys**; current All/Home main totals therefore agree. Multiple legitimate published versions at different venues could also have meaningful per-venue counts. Do not change this shared key globally without an isolated ownership/version fixture and an agreed counting contract. Coordinator/Agent 5 informed; retained as a potential boundary, not a proven production error.

## Cross-review: A4.1 reviewed author replacement

Confirmed the original diagnosis at `scripts/scan_dblp_publications.py:752–760`: automatic source attribution and a validated reviewed target attribution were appended, then staged by `(pid, record key)`, producing two person/paper rows for the same author position. Reviewed key/source/target/signature semantics authorize a scoped correction, not two people for one author. Severity medium for the boundary, confidence high; both current Farhana corrections have unresolved source PID `385/3645`, so current affected records are **zero**.

Coordinator's completed patch changes cache version 6→7 and suppresses only automatic `(source PID, author position)` matches corresponding to **validated** reviewed assignments before adding the reviewed target. Exact diff reviewed: the same source PID's separate real coauthor position survives; rejected duplicate-signature/contradictory-PID/contradictory-ORCID reviews do not suppress automatic assignments. No global name aliases are created. Approved; requested additional exact-signature and rejected-identifier boundary tests beyond the initial three fixtures. No unresolved objection to the scoped patch.

## Cross-review: deleted persistent reviews

Reviewed coordinator's exact changes to `scripts/apply_profile_entity_resolution.py:197` and its tests. Prepass demotes only the module's reviewed profile/alias statuses when the name is absent from **both** review configurations. It clears projected anchor PIDs before recursive aliases are planned; unrelated decisions survive, known-bad exact-profile behavior remains, and cleanup removes only review-added alias candidates. Approved. Existing dependency tests exercise changed-evidence cascading; an explicit deleted-profile-anchor→still-configured-alias cascade regression was suggested as a small additional guard. Current production orphan count was reported zero by the investigator; this review did not independently rescan every review entry.

## Reproduction, results and limits

```
.venv\Scripts\python.exe data/reports/round2/numerical_probe.py
.venv\Scripts\python.exe -m unittest tests.test_home_population tests.test_profile_entity_resolution
```

The probe passes and writes `numerical_evidence.json`; **48** Home/profile Python tests pass. A direct `node tests/test_home_trends.js` attempt could not run because Node is absent from PATH. The coordinator has the established Chrome model harness; this specialist did not claim a new JavaScript suite pass. No new frontend, stylesheet or production export changes were made by this specialist.

Uninspected: independent extraction of all venue-wide totals from the large XML snapshot, publication recall, all historical year completeness, exhaustive browser inspection lists and CSV/Sankey reconciliation (Agent 9), every work-version counting boundary (Agent 5), and historical-population reclassification. Passing sampled computations does not verify all data ownership or the complete dataset.

## Final verification after controlled export promotion

Ran `.venv\Scripts\python.exe data/reports/round2/numerical_final_probe.py` against the promoted `website/data` files and current canonical CSV. It reuses the independent arithmetic in an isolated temporary directory, preserves the original `numerical_evidence.json`, and writes **new** `numerical_final_evidence.json`. Comparison uses `round2/previous_manifests/site_data.js` and `home_summary.json`.

All seven comparisons pass: the complete original numerical evidence is reproduced exactly; the entire Home summary is unchanged; all **648 `(PID, work ID, selected DBLP key, venue, year)` main authorships** are unchanged; all **833 included cohort PIDs** are unchanged; **every venue/year denominator** is unchanged; all **75 annual Home points** and all **ten first-time counts**, trend metadata and growth inputs are unchanged. There are no added or removed included main rows or PIDs. Final counts remain **833 included people, 227 main authors, 533 works, 648 pairs**. Current canonical-CSV and promoted-JS main counts agree independently.

The exported broader PC-candidate population remains **1,114**. Labels change from DB-focused 833 / Limited evidence 225 / ML-focused 27 / Mixed 29 to **833 / 224 / 27 / 30**, consistent with the coordinator's scoped evidence changes. Resolved-people summary changes **1,126→1,125**. This check verifies those totals and absence of unintended main/cohort changes; the identity/source specialists supply the record-specific justification for the broader-population changes.

Promoted Home summary SHA-256 remains baseline **`ec51b48ac54963fd24cd1cef0a4d7485648717f083c4f9e8dd178f3d8b83837d`**. Final promoted site JS is **`4c2c548e91e744cfc964ab59ba283579d925b4dfef55a3b53eeaec33aa47b962`** and canonical CSV is **`06258c1a6f3e01190ae3d3fa93df24d18c986880de35a54d8dd830c5e9a8aa99`**. No production/source changes or commits were made by this verification.
