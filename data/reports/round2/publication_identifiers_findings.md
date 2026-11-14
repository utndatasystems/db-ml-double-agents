# Specialist 4: publication identifiers and ownership

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../../docs/PIPELINE.md) for current behavior.

Initial investigation was read-only: no production inputs or code edited. SQLite used `mode=ro`; only the small probe/report/evidence files in this directory were written. Baseline supplied by coordinator: `567b35b`, branch `audit/backend-correctness-2026-11-06`. No full XML scan or new dump was run.

## A4.1: one reviewed author can retain both source and corrected target attribution

**Severity medium; confidence high on boundary behavior; zero current affected reviewed records.**

`scripts/scan_dblp_publications.py:752-760` computes automatic matches and appends reviewed matches. `:771-808` stages each under `(pid, record_key)`, so ranking only resolves different matches for the same person; it does not resolve the same author position assigned to different people. `reviewed_authors_in_publication` at `:378-396` explicitly allows a source PID to be corrected to its reviewed target. Source/target fields in `data/reviewed_publication_authors.csv` and the documented record-level correction policy indicate replacement of that single author assignment, not adding a second person.

Trigger: both source and corrected target PIDs are resolved service people. Tiny XML: one author `pid="source"` with signature `Short Name`; a valid record-level review maps that exact key/signature from source to target. Expected one author assignment to target. Actual: source gets `dblp_author_pid`, target gets `reviewed_publication_author`; two person-paper rows for one author position. Reproduced by `publication_identifiers_probe.py`, with exact results in `publication_identifiers_evidence.json`.

Production: the two Farhana Choudhury reviews both have source PID `385/3645`, which is not currently resolved. Both correctly produce only the target assignment today. Thus no current population, publication or authorship count change is predicted from the narrow fix. Risk is future identity changes or additional reviewed corrections; do not describe the fixture as a current misassignment.

Proposed fix, pending cross-review: after validating reviewed matches, suppress an automatic source match only at the same author position replaced by that review. Preserve other author positions, unrelated records, contradictory-PID/ORCID rejection and global ambiguity rules. Bump scan cache policy version because corrected outputs differ for affected target sets. Do not turn source signatures into global target aliases. Tests must cover direct-PID and exact-signature source matches, a real source author at another position, and rejected reviews leaving automatic assignments unchanged.

## 15 ORCID inconsistencies: no automatic reassignment justified

All 15 stored rows were located and their author ORCID compared to the read-only candidate person index. All use exact DBLP signature matching, all are side-scope records, and their conflicting ORCID owner is outside the resolved target population. The scanner therefore falls through to the exact signature at `:545-577`; global signature ownership is checked at `:845-848`. This is not proof of erroneous ownership. Replacing all conflicting signature matches with the ORCID owner would lose independently corroborated correct assignments.

| Stored record(s) | Primary evidence inspected | Assessment |
| --- | --- | --- |
| `conf/kdd/FanLFDT26` (Djellel Difallah) | [Author's publication list](https://djelleldifallah.com/) explicitly lists MLaGA and the same coauthors; [DBLP longer-name profile](https://dblp.org/pid/09/10814.html) has ORCID `0000-0002-7513-6047`. | Correct paper/person attribution corroborated; likely split DBLP identity between short and longer names. No blanket merge or reassignment justified from one record. Short-name DBLP page could not be fetched. |
| `journals/pacmhci/ShankarGHP24` (Joseph M. Hellerstein) | [Author manuscript in NSF repository](https://par.nsf.gov/servlets/purl/10531529) identifies Joseph M. Hellerstein, Berkeley, and `hellerstein@berkeley.edu`. Stored conflicting ORCID belongs to Joseph L. Hellerstein. | Strong evidence of erroneous ORCID tag, with correct exact-name assignment. ACM page denied access; NSF search result exposed author manuscript text, subsequent direct fetch timed out. |
| `journals/vldb/ZeakisPSK25`, `conf/esws/ZeakisPSK25` (Dimitrios Skoutas) | [VLDB Journal publisher page](https://link.springer.com/article/10.1007/s00778-024-00879-4) identifies Athena Research Center; [ESWC publisher page](https://link.springer.com/chapter/10.1007/978-3-031-94575-5_17) exposes author list/affiliation anchors. [Conflicting ORCID's DBLP profile](https://dblp.org/pid/s/DimitriosNSkoutas.html) is Dimitrios N. Skoutas, University of the Aegean. | Journal ownership independently corroborated; ESWC author list corroborated but affiliation details not independently expanded. Tag-error explanation is stronger than wrong ownership; do not reassign. |
| `journals/pvldb/ZeakisPSK23`, `journals/corr/abs-2405-14748`, `journals/corr/abs-2312-09864` (Dimitrios Skoutas) | [PVLDB author block](https://www.vldb.org/pvldb/vol16/p2225-skoutas.pdf), [MultiCast author manuscript](https://arxiv.org/html/2405.14748v1), [learned-index author manuscript](https://arxiv.org/html/2312.09864v1) explicitly identify Athena and `dskoutas@athenarc.gr`. | Correct paper/person attribution corroborated independently; conflicting Aegean ORCID appears erroneous. |
| `conf/icde/ZeakisCLS25`, `conf/icde/Skoufis024`, `conf/icde/ChatzigeorgakidisLS24`, `conf/icde/PitsilouPS24`, `journals/corr/abs-2304-12329`, `journals/is/WuTSL23`, `books/sp/21/Chronis0AS21` | Stored title, author list, signature, ORCID, DOI and candidate profiles examined. MultiCast conference/preprint shares title/authors; PVLDB and its preprint share authors/topic, but those are not independent publisher verification for each row. | Same tag-error pattern is plausible, but each row's ownership remains unverified externally in this bounded pass. No rows dropped or moved. |
| `conf/mir/TranBDHHJHRSTZG26` (Bjorn Thor Jonsson 0001) | [Official challenge citation](https://lifelogsearch.org/lsc/data/) confirms exact paper/author list; [coauthor institution's repository](https://research.lancaster-university.uk/en/publications/introduction-to-the-9th-annual-lifelog-search-challenge-lsc26/) lists the same paper; [conflicting-ORCID DBLP profile](https://dblp.org/pid/j/BjornTorJonsson2.html) explicitly distinguishes homonyms. | Homonym collision remains unresolved. Neither official citation nor accessible institutional record uniquely proves which Bjorn authored it. DBLP 0001 page exceeded fetch limit; organisers page denied access. Do not infer identity from shared institutions or bibliography volume. |

ORCID public page returned only JavaScript scaffolding. No official ORCID API data were obtained. Live pages are separately dated corroboration; they do not replace the pinned snapshot for before/after comparisons. The strongest externally corroborated cases show why a universal conflicting-ORCID blocking/reassignment rule would be unsafe. No new identity/publication correction was made from these cases.

## Global ambiguity and direct identifiers

The probe verifies an exact signature owned globally by a target and a non-target PID is skipped, despite being locally unique. Code collects global owners from all interested DBLP person records (`:706-710,:813-819`) and validates staged signatures/ORCIDs (`:835-848`). Direct DBLP author PID is explicitly authoritative (`:512-540`); contradictory names/ORCIDs do not override it. This is an explicit source-trust policy, not demonstrated implementation failure. No production row has direct-PID match basis in this snapshot. Raw match-basis totals: 96,155 exact signatures; 33,122 verified ORCIDs; two reviewed author assignments = 129,279 rows.

Uninspected: exhaustive direct-ID contradiction validation, all global person aliases independently rescanned from 13M-record XML, all publisher metadata for all papers, recall of skipped ambiguous signatures, and whole split-identity bibliographies. A target ORCID that is locally unique but globally duplicated is rejected conservatively; alternative-signature fallback for that condition was not established as a required behavior.

## Cross-review of affiliations and Adam Lee

A3.2 manual affiliation finding approved: `data/README.md:155` requires the researcher's own publications; `reviewed_publication_authors.csv` is the separate reviewed authorship mechanism. Manual affiliation review does not authorize component authorship. Root's patch in `apply_publication_affiliation_evidence` correctly validates `personVersionDblpKeys`, explicit empty ownership, and selected-record legacy fallback. Exact diff reviewed; owned older merged-version case preserved and validation occurs before provenance mutation. Ten affiliation tests passed, plus two Adam reviewed-identity tests.

The related automatic DOI candidate leak is real at `scripts/automatic_affiliations.py:39-45`; refresh serialization in `scripts/refresh_affiliations.py:32-34` currently omits ownership keys. Two exposed unowned DOIs are recorded by A3; neither produced an accepted wrong affiliation. A safe fix needs ownership serialization plus matching selected-record fallback and a `POLICY_VERSION` change, otherwise offline reuse at `refresh_affiliations.py:51-56` may preserve stale evidence. Not edited in this investigation.

Adam Lee completed correction approved: `data/reviewed_person_identities.csv` scopes the old PID `58/8815` to target `62/1373` with Pittsburgh label and primary institution/publication evidence. No publication transfer is authorized. `tests/test_reviewed_identity_evidence.py` checks success and changed-affiliation rejection; reviewed and run with affiliation tests (12 passed). Broader shared test suite left to coordinator.

## Reproduction

`.venv\Scripts\python.exe data/reports/round2/publication_identifiers_probe.py`

`.venv\Scripts\python.exe -m unittest tests.test_reviewed_identity_evidence tests.test_publication_affiliations`

The first command writes only the small adjacent evidence JSON, performs read-only production SQL/CSV reads and scans two tiny temporary XML fixtures. No production mutation, refresh, classification change or frontend edit occurred in this initial pass.
