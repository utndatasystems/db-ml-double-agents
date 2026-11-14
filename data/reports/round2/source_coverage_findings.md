# Specialist 1: source extraction and service coverage

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../../docs/PIPELINE.md) for current behavior.

Initial pass bounded to SIGMOD 2022/2024/2025 research rosters, 2022/2024 officer and PODS rosters, and VLDB 2025 review board. Eight small official pages were fetched into this temporary directory. No production database was edited by this specialist; SQLite connections use `mode=ro`. HEAD at investigation: `567b35b`. Full original request and previous audit/baseline were read.

## Confirmed R2-S1: name-only source merge discards differing affiliations

Severity medium; confidence high. `scripts/scrape_sigmod_reviewers.py:404` previously deduplicated side-track rows on role and name only. `:555` previously deduplicated all merged rows on year, role, round and name only. This contradicts the scraper's documented contract (`:6-11`): raw service records precede identity resolution, and `name_key` is only for candidate generation. A common name cannot establish that two source observations describe one person. Whether differently affiliated rows ultimately describe homonyms or one changing/dual affiliation does not justify discarding the evidence.

Official source evidence and actual production impact:

- [SIGMOD 2024 research PC](https://2024.sigmod.org/org_sigmod_pc.shtml), saved HTML lines 554-555: Chao Zhang at University of Waterloo and Tsinghua University. Parser 254 rows -> old merge 253. Stored data omits Tsinghua.
- [SIGMOD 2025 research PC](https://2025.sigmod.org/org_sigmod_pc.shtml), saved HTML lines 519-520 and 595-596: Chao Zhang at Waterloo and Tsinghua; Jia Yu at Washington State and Wherobots Inc. Parser 314 rows -> old merge 312. Stored data omits Tsinghua and Wherobots.
- SIGMOD 2022 research PC: 209 parsed and stored, no such loss in this sample.
- All three research page SHA-256 values equal the corresponding production `source_pages` hash. This is a fixed-source implementation error, not a changed-page comparison.

Small fixture: a chair and two Chao Zhang rows at distinct institutions parse as three rows, old merge yields two, expected three. Regression additionally exercises both official Chao/Jia patterns, exact duplicate collapse regardless of source order, and side-track pre-merge collisions. Both new tests fail against the baseline HEAD module (3 vs expected 5; 1 vs expected 2) and pass with the fix. `python -m unittest tests.test_sigmod_parser tests.test_service_roles`: 11 pass.

Fix (authorized exclusive ownership): include the full normalized affiliation in both keys; retain existing institution-aware PODS chair/member suppression. New code comments explain why name equality is insufficient. No resolution threshold or identity assignment is changed. Candidate side effect: differing affiliation renderings from complementary sources can survive as separate source observations. This conservatively preserves evidence; identity and distinct service-year calculations remain downstream. Current Chao Zhang is unresolved (`ambiguous_exact_name`, 119 exact + 8 variant candidates). Jia Yu currently resolves to `86/3457-20` using full-coverage Washington State evidence; adding Wherobots must be passed through the ordinary guarded identity stages rather than retaining that decision artificially.

Cross-review: Agent 2 independently confirmed the missing records and current identity states, challenged country-label duplicates and Jia's identity consequences, then approved the completed parser diff and tests. Reviewer explicitly preferred retaining differing country/campus organization evidence rather than adding fuzzy equivalence to the global key. The existing narrow PODS guard remains unchanged. The coordinator owns same-input identity replay, particularly Jia; no disagreement remains about the extraction fix.

## Prior PODS correction: now reproducible against identical source bytes

The already-fixed officer parser correctly reads Pablo Barceló as PODS program chair (2022) and Wim Martens as chair (2024). Four fresh officer/PODS pages hash-match stored production source hashes exactly. Replaying them restores Pablo's missing chair row (+1) and replaces Wim's ordinary member row with chair (net 0). This closes the previous audit's source-availability limitation. It does not by itself establish their resulting population classifications; the normal downstream pipeline must be rerun.

Full corrected merging of the selected years also removes an existing duplicate Benny Kimelfeld 2025 PODS ordinary-member row because the same institution already supplies an explicit PODS chair row. This is an intended consequence of the prior PODS chair/member deduplication rule; no new external evidence or identity choice is involved.

Planned service observations with fixed source inputs: SIGMOD 2022 313->314; 2024 346->347; 2025 423->424. Overall all-venue observations predicted 4,666->4,669: three restored different-affiliation records, Pablo +1, Wim net 0, Benny duplicate -1. These are a temporary replay, not production changes. Each record-level delta is in `source_coverage_evidence_after_fix.json`, and full planned year rows are in `sigmod_2022_planned_records.json`, `sigmod_2024_planned_records.json`, `sigmod_2025_planned_records.json`.

## Representative check and limits

VLDB 2025 official Volume 18 review board parses 375 rows; all 375 match stored year/role/name/affiliation records. Review-cycle year mapping and EIC/review-board roles were inspected in code. A listed committee role proves listed service, not completed reviewing activity.

No exhaustive role-by-role re-extraction of ICDE, EDBT or CIDR was performed. Same-name keys also appear in ICDE/EDBT merge paths, but no production source loss there was demonstrated, so these are follow-up inspection targets rather than confirmed data-impact claims. No independent pagination/alternate-page crawl across all years, exhaustive Shadow PC audit, or proof of all upstream name recall was performed. No new large snapshots/full refresh were run.

Evidence: `source_coverage_evidence.json` preserves the pre-fix reproduction; `source_coverage_evidence_after_fix.json` records corrected replay; `source_coverage_probe.py` is a read-only reproduction script using saved official HTML. The coordinator owns final production refresh, integration, commits, combined testing and report.
