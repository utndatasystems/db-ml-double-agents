# Specialist 6 — comparable publications and focus eligibility

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../../docs/PIPELINE.md) for current behavior.

Baseline: `audit/backend-correctness-2026-11-06`, HEAD `567b35bd11d7f9c686cb66570d00409e9cb0f35d`; fixed stored DBLP 2026-10-03 inputs. Initial investigation was read-only; later exclusive ownership covered only `data/focus_publication_exclusions.csv` and `tests/test_focus_population.py`. No identities selected by publication counts, no threshold changed, no production export written by this specialist.

## Confirmed bug: long official tutorials supply research evidence

Severity medium; confidence high. `scripts/focus_population.py:98` permits selected-proceedings records of six or more pages unless their title or reviewed key matches an exclusion. `data/focus_publication_exclusions.csv:2`–`:24` had only narrow title rules and seven key reviews. Official SIGMOD 2022 tutorials with neutral titles therefore counted as full DB papers. The intended selected-research policy is documented in README.md:18 and the classifier's exported `dbPaperPolicy`.

Primary evidence: https://2022.sigmod.org/sigmod_tutorials.shtml names eight unique tutorials (one entry is duplicated on the page). https://dblp.org/db/conf/sigmod/sigmod2022.html supplies exact keys and pagination. The SIGMOD-hosted 2008 proceedings https://sigmod.org/publications/dblp/db/conf/sigmod/sigmod2008.html puts `conf/sigmod/PeiHTL08` under Tutorials; its stored title explicitly ends “tutorial summary”.

Expected: these records remain browseable, with zero DB-focus contribution. Actual baseline: seven distinct tutorial keys contribute 18 researcher–work pairs for 18 PC candidates. Six keys are 2022 tutorials (16 pairs); PeiHTL08 adds two pairs. The four-page BharadwajC22 item already contributes zero; 0001Z22 (HTAP tutorial) is absent from these stored researchers' publications. Nine exact-key reviews document the whole bounded official list plus PeiHTL08; they preserve the six-page research threshold and avoid broader title filtering.

Fix: `data/focus_publication_exclusions.csv:25`–`:33`. Regression `tests/test_focus_population.py:105` checks NargesianAJ22 exclusion despite its neutral title, preserves an unrelated research key with the same title, and exercises complete classifier behavior: removing tutorial evidence changes a boundary fixture from the previously possible 4 DB/2 ML to 3 DB/2 ML, Mixed. The general reviewed-key test validates all 16 reviewed exclusions and provenance.

Same-input full candidate recalculation using `load_publications()` plus snapshot metadata cache: baseline matches every stored candidate profile exactly. After the fix, DB paper count decreases by one for each of 18 people, with no focus-label changes: 833 DB-focused, 27 ML-focused, 29 Mixed, 225 Limited evidence, 1,114 PC candidates. Exact people, counts, years and reviewed rows are in `focus_evidence.json`; reproduction: `.venv\Scripts\python.exe data/reports/round2/focus_probe.py --compare`. This is a fixed-input classifier experiment, not a source refresh. Coordinator owns production export regeneration.

Cross-review: specialist 5 independently checked the official tutorial grouping, exact-key approach, and selected stored authorships; approved, requesting exact Pei review rather than a broad title rule. Specialist 6 reviewed specialist 5's resolver guard and approved dynamic archival-root tracking, native rel/DOI controls, and unchanged current corpus counts.

## Checks and limits

`python -m unittest tests.test_focus_population`: 18 tests pass. Existing ratio fixtures cover exact 2:1 versus Mixed, five-paper/three-year minimum, repeated-service fallback, distinct service years, distinct DB publication years, duplicate works, nonqualifying/general/shadow roles and owned-version evidence. Selected venue rules require exact publication channels; workshops, companions, ordinary VLDB Journal/TODS, short and unknown pagination have explicit controls. All 14,311 cached selected-record metadata entries have empty `publtype`; no actual informal/withdrawn selected-record case was found.

Inspected real boundary profiles include Doris Xin (5 DB, 3 publication years, 1 service year: DB-focused), Brad Glasbergen (5 DB, 2 publication years, 1 service year: Limited), Abdul Wasay (2 DB/1 ML, 2 DB years, 1 service year: Limited), Michele Linardi (2 DB/1 ML, only 1 DB year, 2 service years: Limited), Medha Atre (2 DB, 2 publication years, 3 service years: fallback), and Pedro Bizarro (4 DB/2 ML: exact 2:1). These match stated policy; this does not independently verify their identities or every publication.

A lexical scan flagged 54 counted titles containing tutorial/demo/challenge/invited/reproducibility terms; many are ordinary research. This pass verified the bounded 2022 list and Pei example, not every suspicious title or every older proceedings track. Additional long tutorials can remain (for example SIGMOD 2017 Kumar0017 has author/IBM tutorial evidence); a comprehensive track-based review is follow-up, not a claim of complete nonresearch exclusion. Unpaginated CIDR semantics, every PACMMOD/PVLDB issue, publication-type combinations and all historical venue label variants were not externally verified.
