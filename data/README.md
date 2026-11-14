# Pipeline data

This folder holds source snapshots, working databases, reviewed decisions and audit outputs. [The pipeline guide](../docs/PIPELINE.md) explains processing order and inclusion rules.

## Working files

| Path | Purpose |
| --- | --- |
| `*_reviewers.sqlite` | Saved committee entries and source-page metadata for each venue |
| `entity_resolution.sqlite` | Name groups, DBLP candidates and accepted identities |
| `snapshot_sources.json` | Selected monthly DBLP release, its DTD and the ROR release/checksums |
| `dblp_snapshot/`, `ror_snapshot/` | Downloaded snapshots and the derived ROR index |
| `dblp_publications_for_db_people.csv`, `*.csv.scan.json` | Raw person/publication links from DBLP and the scan's input fingerprint |
| `dblp_main_ml_venue_year_totals.csv` | Conference-wide ML paper counts used as denominators |
| `dblp_publication_entities_for_db_people.csv` | Deduplicated person/work links used by the website builder |
| `publication_entity_resolution.csv` | DBLP record-to-work mapping |
| `affiliation_metadata.sqlite`, `automatic_affiliation_evidence.json` | Cached publisher responses and generated affiliation evidence |
| `.pipeline_state.json` | Local orchestration state used to decide which stages need rerunning |

For website build exports, see [website data](../website/README.md#private-build-exports).

SQLite databases, large snapshots and publication corpora stay local. Do not treat a working database as the only record of a manual decision.

## Reviewed identity and paper decisions

These tracked inputs preserve manual and agent-assisted findings across rebuilds:

| File | Use |
| --- | --- |
| [reviewed_person_identities.csv](reviewed_person_identities.csv) | Link a service name to a DBLP person using scoped affiliations and dated sources |
| [reviewed_service_aliases.csv](reviewed_service_aliases.csv) | Link a roster spelling to an already accepted service identity |
| [reviewed_publication_authors.csv](reviewed_publication_authors.csv) | Correct an individual paper/signature assignment without merging entire profiles |
| [publication_affiliation_evidence.csv](publication_affiliation_evidence.csv) | Record an affiliation verified from a specific authored publication |
| [publication_category_mappings.csv](publication_category_mappings.csv) | Assign display categories by venue text, DBLP namespace or exact paper key |
| [publication_category_reviews.csv](publication_category_reviews.csv) | Preserve paper-level evidence and unresolved category questions |
| [review_attributions.csv](review_attributions.csv) | Record whether a specific saved decision was AI-assisted, human or a project review |
| [focus_venue_rules.csv](focus_venue_rules.csv) | Define which DB proceedings and paper lengths supply focus evidence |
| [focus_publication_exclusions.csv](focus_publication_exclusions.csv) | Exclude known nonresearch items from focus evidence, retaining them as context |

Keep the existing CSV headers and add a source, reason and review date where required. After editing, use [the pipeline rebuild](../docs/PIPELINE.md#run-it).

The website labels reviewed decisions in person and publication info panels, with dates and evidence links. Attribution records bind to the SHA-256 of the complete decision row (`scripts/review_provenance.py`); changed decisions require their attribution to be checked again. Existing AI-assisted reviews are recorded explicitly. New decisions without an attribution use the neutral “Project review” label.

- **Identities:** verify the person against primary sources, not publication volume. Reviews are scoped to candidate PIDs, previous assignments and observed affiliations. Changed evidence can require review again. Service aliases do not create publication aliases.
- **Authorship/affiliations:** use exact publication keys and author-specific evidence. See [matching and counting rules](../docs/PIPELINE.md#matching-and-counting-rules) for how the pipeline validates authorship and selects affiliations.
- **Categories:** exact mappings precede namespace prefixes; inclusive year bounds can keep an exception narrow. Prefix rules require a complete DBLP namespace ending in `/`. Main ML and preprint recognition remain protected. See [classification rules](../docs/PIPELINE.md#matching-and-counting-rules) for the distinction between display categories and study eligibility.
- **Category reviews:** `confirmed` means the recorded evidence settles the assignment; `pending` preserves an open question. Changed metadata or a conflicting confirmed category stops the category audit for rechecking. Update the mapping and review together when needed.

## Sharing and rebuilding

| Keep in Git | Keep locally, ignored by Git |
| --- | --- |
| All reviewed inputs and rules listed above | Downloaded DBLP/ROR snapshots and working SQLite databases |
| `snapshot_sources.json` with release URLs and checksums | Large publication CSVs and their scan metadata |
| `automatic_affiliation_evidence.json`, `dblp_main_ml_venue_year_totals.csv` and identity inspection exports (`dblp_name_resolution.csv`, `dblp_candidates.csv`, `invalid_reviewer_names.csv`) | Supporting caches, generated reports, backups and `.pipeline_state.json` |

The generated files kept in Git record the build's evidence and decisions; edit the reviewed inputs rather than these exports. Historical tracked audit reports are exceptions to the ignored `reports/` directory. For generated browser assets, see [website files](../website/README.md).

See [local viewing](../README.md#run-locally) and [pipeline refresh modes](../docs/PIPELINE.md#run-it) for commands. Offline rebuilds require the local inputs above. Live sources can change, so a new online run need not reproduce an old build byte for byte.

For an exact handover, retain a versioned archive of the selected source downloads, working databases, publication CSVs and supporting caches listed below. Record the matching code commit and dependency versions. Older downloaded releases can stay local; `snapshot_sources.json` selects the active ones.

## Audits and cleanup

`dblp_name_resolution.csv`, `dblp_candidates.csv` and `invalid_reviewer_names.csv` expose identity decisions, alternatives and rejected parser names. Current files under `reports/` have these roles:

`reviewer_scraper_repairs.csv` records applied source-name corrections; the rules live in `scripts/scraper_repairs.py`.

| Files | Purpose |
| --- | --- |
| `arxiv_candidate_metadata_cache.json` | Downloaded arXiv evidence used by version resolution and website provenance |
| `focus_snapshot_metadata.json`, `publication_category_snapshot_evidence.json` | Cached DBLP evidence for focus classification and category validation |
| `focus_publication_audit.csv` | Generated focus decisions, also read by coverage exports |
| `*_entity_resolution_plan.*`, `unresolved_entity_resolution_*` | Generated matching decisions and unresolved-candidate review queues |
| `publication_entity_candidates*.csv`, `publication_category_*.csv`, `publication_category_summary.json` | Generated paper-version and category review outputs |
| `affiliation_cleanup_report.*`, `primary_affiliation_review.csv` | Generated affiliation cleanup and inspection reports |

Keep the caches for offline processing. The remaining historical subfolders hold audit evidence, saved source pages/PDFs and recovery copies; they are not current pipeline stages. Do not delete `reports/` wholesale.

- Backups live in `backups/`; retain recovery copies and source caches when comparing old builds. Scratch files and old screenshots are not operational review inputs.
- For generated browser-file retention, see [generated data](../website/README.md#loading).
- Tracked [audit reports](reports/backend_correctness_audit_round2.md) document historical investigations. Their counts, code references and test results are not current dataset guarantees.

## Coverage interpretation

Coverage Sankeys start at saved scraper output and distinguish service entries, name groups, people, author links, publication records and works. Percentage mode normalizes each stage separately; merges are not rejected people or papers. CSV exports preserve stage-specific evidence. The flows cannot measure names missed before parsing, and review flags are not confirmed errors.
