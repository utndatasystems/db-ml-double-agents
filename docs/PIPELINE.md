# Data pipeline

`run_pipeline.py` refreshes the sources, resolves people and papers, builds the website and runs checks. For local viewing, see [Run locally](../README.md#run-locally).

## Run it

From the repository root, after [setup](../README.md#run-locally):

```sh
python run_pipeline.py
```

For a full refresh:

```sh
python run_pipeline.py --full-refresh
```

| Option | Use |
| --- | --- |
| `--full-refresh` | Latest monthly snapshots, forced identity/publication scans and fresh supporting metadata |
| `--keep-snapshots` | Keep the recorded DBLP/ROR releases; still refresh live committee pages and supporting evidence as needed |
| `--offline` | Rebuild from existing local sources and caches without network requests |
| `--dry-run` | Print the planned commands without executing stages |
| `--years 2024 2025` | Refresh only these service years; does not change the study's inclusion window |

Normal runs reuse valid intermediate results. `--full-refresh` cannot be combined with `--offline` or `--keep-snapshots`.

## Processing order

All scripts below are in `scripts/`.

| Stage | What happens | Main implementation |
| --- | --- | --- |
| 1. Snapshots | Select and checksum-verify the latest monthly DBLP dump, its DTD and the latest ROR release; record them in `data/snapshot_sources.json` | [refresh_snapshots.py](../scripts/refresh_snapshots.py) |
| 2. Committee sources | Scrape the five DB venues into SQLite databases and validate venue/year coverage | `scrape_*_reviewers.py`, [report_service_coverage.py](../scripts/report_service_coverage.py) |
| 3. People | Group service names, find DBLP candidates and apply guarded identity rules plus reviewed corrections | [enrich_reviewers_with_dblp.py](../scripts/enrich_reviewers_with_dblp.py), `apply_*_entity_resolution.py` |
| 4. Publications | Collect DBLP papers for resolved people and conference-wide ML paper totals | [scan_dblp_publications.py](../scripts/scan_dblp_publications.py) |
| 5. Versions | Merge supported versions, fetch missing arXiv evidence, resolve again and rebuild the remaining review queue | [resolve_publication_entities.py](../scripts/resolve_publication_entities.py), [enrich_publication_candidates_with_arxiv.py](../scripts/enrich_publication_candidates_with_arxiv.py) |
| 6. Validation and affiliations | Revalidate publication-category reviews and refresh author-specific publisher affiliation evidence | [audit_publication_categories.py](../scripts/audit_publication_categories.py), [refresh_affiliations.py](../scripts/refresh_affiliations.py) |
| 7. Website | Apply DB-focus classification; update browser data and the crossover matrix; validate references and consistency; keep readable build exports separate | [build_site_data.py](../scripts/build_site_data.py), [build_matrix_stats.py](../scripts/build_matrix_stats.py), [validate_static_site.py](../scripts/validate_static_site.py) |
| 8. Checks | Run Python tests and, if Node.js is available, check `app.js` syntax | [run_pipeline.py](../run_pipeline.py) |

Browser interaction checks are separate; see [frontend checks](../website/README.md#check-changes).

## Who qualifies

Scrapers default to **2022 through the last completed calendar year**. The study currently uses qualifying **2022–2025** service at **SIGMOD/PODS, VLDB, ICDE, CIDR or EDBT**.

The raw service pool is broader than the study population. Inclusion accepts research, industry and demo PCs, review boards, senior reviewing roles and program leadership. Shadow PCs, general chairs, awards and tutorial/workshop service alone do not qualify. Committee membership does not establish how many papers someone actually reviewed.

A matched person is **DB-focused** when they have at least twice as many qualifying DB as ML conference papers, plus either:

- Five qualifying papers across three publication years; or
- Two DB papers in two publication years and qualifying PC service in two distinct years.

Classification uses all recorded publication years and deduplicated works. DB evidence uses selected main proceedings, including PVLDB and PACMMOD: at least six pages where paginated, with unpaginated CIDR papers allowed. Workshops, companion proceedings, ordinary journals and known nonresearch items are excluded; missing length evidence does not qualify.

The rules live in [home_population.py](../scripts/home_population.py), [focus_population.py](../scripts/focus_population.py), [focus_venue_rules.csv](../data/focus_venue_rules.csv) and [focus_publication_exclusions.csv](../data/focus_publication_exclusions.csv). These labels describe the selected venues, not seniority or all of DB/ML research.

## Matching and counting rules

- **People:** DBLP PID is the identity key. Unique exact aliases are followed by high-precision, ROR, alias/two-anchor, complete-affiliation and profile-correction passes. Uncertain candidates stay unresolved; publication volume alone never selects a person.
- **Authorship:** direct PID, globally unique verified ORCID or globally unique exact author signature. Reviewed exceptions apply only to specific paper/signature pairs. No fuzzy author matching.
- **ML papers:** exact main-conference NeurIPS/NIPS, ICML or ICLR records. Workshops do not count. Conference-wide denominators come from the same DBLP snapshot, not the cohort's bibliography.
- **Versions:** automatic merges use explicit DBLP version links, shared DOIs or guarded exact-title/author rules. Cached arXiv DOI/current-title evidence can link only to existing archival DBLP records. Conflicting or fuzzy evidence stays for review. A researcher is credited only with versions they actually authored.
- **Counts:** global paper charts count a shared work once; author charts count each researcher's participation. Relative paper share is cohort papers divided by all main papers at that venue. Missing totals are unavailable, not zero; cumulative Relative excludes those years from both sums.
- **Affiliations:** use the newest usable author-specific publisher evidence, with reviewed evidence and DBLP/service fallbacks. ROR helps reconcile organization names but does not supply display affiliations. A publication affiliation is not proof of current employment.

Display categories are separate from DB-focus eligibility. Editing a category mapping cannot turn a workshop into a qualifying ML paper.

## Related guides

- [Website files, loading and checks](../website/README.md)
- [Review files and validation](../data/README.md#reviewed-identity-and-paper-decisions)
- [Sharing and reproducibility](../data/README.md#sharing-and-rebuilding)
- [Static hosting](../HOSTING.md)

Source releases, matching evidence and an input fingerprint accompany the generated data. Home and publication charts use the DB-focused cohort; classification and coverage views also show excluded or unresolved records. Chart formulas and time windows are in the website's Methodology.

## If a run stops

- **Coverage loss:** inspect the source page and parser output; do not accept a smaller roster without checking it.
- **Changed review evidence:** update the tracked review only after rechecking its sources.
- **Stale or missing inputs:** use the [pipeline rebuild](#run-it) to refresh dependencies in order. Do not bypass freshness checks just to finish a build.

For audit locations and retention, see [audits and cleanup](../data/README.md#audits-and-cleanup).
