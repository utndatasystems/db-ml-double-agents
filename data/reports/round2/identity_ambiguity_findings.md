# Specialist 2: identity resolution and ambiguity

> Historical audit record. Counts, code references and test results describe the recorded revisions, not the current dataset. See the [pipeline guide](../../../docs/PIPELINE.md) for current behavior.

Read-only starting evidence used HEAD `567b35bd11d7f9c686cb66570d00409e9cb0f35d`, the pinned DBLP/ROR sources and SQLite `mode=ro`. No production database was applied by this specialist. The coordinator subsequently authorized only the scoped review row and its regression tests.

## Confirmed incorrect service identity: Adam Lee

Severity medium; confidence high. `scripts/enrich_reviewers_with_dblp.py:1435` accepts the unique exact alias without independent identity evidence. This remains the deliberately established base rule; the proposed correction does not change that rule or force every sparse profile to a fuller candidate.

Stored `adam lee` resolves to `58/8815`, with one SIGMOD 2022 PC observation at University of Pittsburgh, while candidate `62/1373` is Adam J. Lee. Both PIDs are in the pinned candidate index; the accepted short PID has neither affiliation nor ORCID metadata. Pittsburgh's [institutional homepage](https://people.cs.pitt.edu/~adamlee/) identifies Adam J. Lee. Its [publications page](https://people.cs.pitt.edu/~adamlee/pubs/index.html) directly links his DBLP bibliography and independently identifies the same 2022 DIS article and named coauthors as [PID 62/1373](https://dblp.org/pid/62/1373.html), record `conf/ACMdis/JainFL22`. The [official roster](https://2022.sigmod.org/org_sigmod_pc.shtml) supplies the short service spelling and Pittsburgh affiliation. Publication volume and focus were not used to select the identity.

Expected: Pittsburgh's service observation belongs to `62/1373`. Actual: `58/8815`. Existing automatic profile correction (`scripts/apply_profile_entity_resolution.py:137`) cannot settle this because the fuller DBLP candidate lacks affiliation notes and there is only one service event; relaxing those guards is unnecessary.

After cross-review by specialist 3, added `data/reviewed_person_identities.csv:17`, scoped to previous PID `58/8815`, target `62/1373`, the exact observed Pittsburgh affiliation and review date 2026-11-07. The existing reviewed-decision validator checks the candidate and affiliation scope after future refreshes. No publications belonging to the old PID are transferred by this correction. There is no second accepted group for the target PID, so swapping this one service identity preserves the number of assigned groups and distinct PIDs; publication and focus consequences require the coordinator's controlled refresh.

Reproduction: `.venv\Scripts\python.exe -m unittest tests.test_reviewed_identity_evidence tests.test_profile_entity_resolution` passes 33 tests. The two new tests require the production manifest row, reproduce the expected proposal from the stored sparse-exact shape, and reject an out-of-scope affiliation. Before adding the review row the new tests fail because no evidence-backed decision exists. Small stored-data evidence and the counterfactual proposal are in `identity_ambiguity_evidence.json`.

## Sampling and limits

The actual sparse-exact queue contains 33 groups. Adam was externally resolved; Ahmed Aly, Ahmed Mahmood, Brian Cooper and Yisu Wang were inspected as candidate sets but were not reassigned. Their fuller candidates can be plausible or split identities; missing affiliation notes do not prove the accepted PID is a different person. Current `candidate_unique_variant` samples include Aecio Santos, Dana M Van Aken and Angelos Christos Anadiotis. Missing or conflicting affiliation evidence leaves these held under existing rules. Chao Zhang remains ambiguous (119 exact plus 8 variant PIDs), with Tsinghua, Lyon 1 and Waterloo affiliations; no identity was forced.

There are 27 `unresolved_no_local_dblp_candidate` statuses. Counting groups with an empty *real-person* candidate list yields 37 because nonperson disambiguation blockers are held separately. Examples were inspected, including HV Jagadish and reversed-name Ishikawa Yoshiharu, but the bounded pass did not establish an evidence-backed alias correction for them. The count distinction should be retained when reporting candidate coverage.

Code examined normalization (`enrich_reviewers_with_dblp.py:208`), candidate generation (`:1102`), exact acceptance (`:1435`), affiliation competitor guards in high-precision/ROR/next-tier stages, and profile corrections (`apply_profile_entity_resolution.py:116`). A comparison of accepted unique-variant/existing-PID decisions found no incompatible full middle-name pattern in the current stored dataset; a dotless-i normalization variant was the only compatible-name check exception. This does not establish normalization completeness.

## Cross-review supplied

Specialist 1's source deduplication finding is confirmed: `merge_sigmod_records()` had omitted affiliation from its seen key, discarding actual homonyms or differing source evidence. Adding full normalized affiliation preserves both listed rows while exact repeated records still collapse, and keeps the separate PODS chair/member institution guard. Chao remains unresolved; restoring Jia Yu's Wherobots label must be replayed through identity rules, because full affiliation coverage can honestly change an earlier acceptance. [Jia Yu's own page](https://jiayuasu.github.io/) explicitly documents Wherobots, former Washington State service and the SIGMOD 2025 PC invitation, which supports the same person independently; no manual change was made on this evidence alone.

Specialist 3's deleted-review finding is confirmed for direct invocation: absent entries bypass revalidation and preserve the module's previous reviewed status. Normal pipeline reindexing can mask this path. A narrow fix should demote only module-owned reviewed assignments, preserve unrelated decisions, recompute dependent aliases against the final plan, and remove only review-added alias links. Restoring a known-unreliable previous exact PID would be unsafe. No current production review entry was found missing; deleted-entry effects are a counterfactual regression, not a claimed current population error.

Uninspected: exhaustive external identity verification, every accepted exact alias, new DBLP snapshots, and publication consequences of changing Adam's service PID. The other 32 sparse profiles remain review flags, not established wrong-person assignments.
