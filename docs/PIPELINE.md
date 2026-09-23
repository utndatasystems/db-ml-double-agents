# Data pipeline and identity model

This document describes the current production pipeline used by the Database × Machine Learning Explorer. It is intentionally limited to behavior implemented in the repository.

## 1. Pipeline overview

```text
Official DB service pages
        │
        ▼
venue reviewer SQLite databases
        │
        ▼
reviewer name → DBLP person resolution
        │
        ▼
entity_resolution.sqlite
        │
        ├───────────────┐
        │               │
        ▼               ▼
DBLP person metadata    DBLP paper-like records
        │               │
        └───────┬───────┘
                ▼
dblp_publications_for_db_people.csv
                │
                ▼
website/data/site_data.*
website/data/matrix_stats.*
```

Database service and publication evidence are joined only through resolved DBLP PIDs.

## 2. Raw DB-service evidence

The venue scrapers write:

```text
data/cidr_reviewers.sqlite
data/edbt_reviewers.sqlite
data/icde_reviewers.sqlite
data/sigmod_reviewers.sqlite
data/vldb_reviewers.sqlite
```

These databases contain service observations rather than entity-resolved people. The same person can appear multiple times across years, roles, or affiliations.

Known source/parser artifacts are handled in derived staging by `scripts/scraper_repairs.py`; the raw venue databases are not rewritten by the entity-resolution step.

`run_scraper_repair_audit.bat` runs the staging/repair pass without performing the full DBLP scan and writes the scraper-repair audit CSV.

## 3. Reviewer → DBLP identity

`scripts/enrich_reviewers_with_dblp.py` builds `data/entity_resolution.sqlite` from the reviewer databases and the pinned DBLP snapshot.

The pinned files are defined in that script:

```text
dblp-2026-09-01.xml.gz
dblp-2023-06-28.dtd
```

The automatic resolution rule is conservative: a reviewer group is assigned a PID only when its normalized exact alias resolves uniquely. First/last-name variants and ambiguous candidates are retained for audit rather than automatically merged.

The entity database stores:

- staged reviewer observations;
- provisional reviewer name groups;
- DBLP candidate people;
- DBLP person aliases, affiliations, verified ORCIDs, and snapshot provenance;
- final resolution status and `resolved_dblp_pid`.

`scripts/apply_high_precision_entity_resolution.py` implements a separate guarded pass. It is dry-run by default. With `--apply`, it writes only the proposals produced by its high-precision rules and creates a backup first.

The website treats `resolved_dblp_pid` as the identity authority.

## 4. Current/former DBLP affiliations

Fresh DBLP person ingestion stores three affiliation views in `dblp_people`:

- all observed affiliation notes;
- current affiliation notes;
- former affiliation notes.

`scripts/refresh_current_affiliations.py` exists to update an older entity database from the same pinned local snapshot. It adds/updates only the current/former affiliation fields and does not alter resolved PIDs.

The explorer prefers the current DBLP affiliation when present. If no current DBLP affiliation is available, the site-data builder falls back to the latest observed DB-service affiliation. The person popup retains the broader affiliation history.

## 5. Publication ingestion

`scripts/scan_dblp_publications.py` is the only publication-ingestion step used by the website.

Inputs:

```text
data/entity_resolution.sqlite
data/dblp_snapshot/<pinned XML + DTD>
```

Output:

```text
data/dblp_publications_for_db_people.csv
```

Only DBLP `article`, `inproceedings`, and `incollection` records are included.

### Publication-author identity

The scanner starts from already-resolved DBLP PIDs and accepts publication links using the strongest available evidence:

1. direct DBLP author PID when present;
2. verified ORCID matching the resolved DBLP person;
3. exact case-sensitive DBLP author signature matching a stored alias.

For ORCID/signature links, the scanner also reads DBLP person records and requires the relevant value to be globally owned by the target PID. Ambiguous ownership is skipped.

No fuzzy publication-author matching is used.

## 6. Main vs side publication scope

Every retained publication has an internal `scope`.

### `main`

`main` is reserved for exact main-conference NeurIPS/NIPS, ICML, or ICLR records.

The scanner prefers DBLP stream metadata. Its fallback requires a canonical main-conference booktitle together with the matching DBLP conference-key prefix. Explicit workshop wording prevents main classification.

Only `main` papers can establish DB × ML crossover evidence.

### `side`

Every other included paper-like DBLP record for the same resolved person is `side`.

`side` is broad bibliography context. It is not synonymous with workshop or ML.

## 7. Presentation publication classes

`scripts/build_site_data.py` adds a separate descriptive `publicationClass`:

```text
main_ml
database
other_conference
journal
workshop
preprint
other
```

The class is derived from metadata already present in the publication CSV. It does not change `scope` and does not affect crossover membership.

Important conservative rules implemented in the builder include:

- `main` always maps to `main_ml`;
- explicit workshop wording maps to `workshop`;
- CoRR maps to `preprint`;
- known database venues use an explicit DBLP-key mapping;
- remaining `article` records map to `journal`;
- remaining `inproceedings` records map to `other_conference`;
- remaining records fall back to `other`.

## 8. Website dataset

`scripts/build_site_data.py` joins:

```text
entity_resolution.sqlite
+
dblp_publications_for_db_people.csv
```

by exact DBLP PID.

`website/data/site_data.json` contains **every resolved DB researcher**, including researchers with zero main-ML papers. Each person record includes DB service, affiliations, aliases/identifiers, and the DBLP publication records linked to that PID.

The front end does not expose an all/core scope selector. Instead:

- the default explorer contains only researchers with at least one main-ML paper;
- an explicit researcher-name search may surface any resolved DB researcher;
- ML venue/year filters still require exact main-ML activity.

## 9. Matrix

`scripts/build_matrix_stats.py` uses only `scope=main` papers.

For each all-years DB venue × ML venue cell:

```text
numerator   = distinct resolved people with service at DB venue D
              and at least one main paper at ML venue M

denominator = distinct resolved people with service at DB venue D
```

The website displays this matrix as a read-only all-years summary. Explorer filters do not change it and matrix cells are not filter controls.

The builder also emits year-indexed matrix data using main-ML publication year while keeping the DB-service denominator as the all-years resolved roster. The current front end uses the all-years matrix.

## 10. Front-end timeline/filter semantics

The main page is intentionally restricted to the core signal:

- global Publication activity: main NeurIPS/ICML/ICLR papers only;
- explorer mini timeline: main ML only;
- ML venue filter: exact main ML venue;
- ML publication-year filter: exact main-ML paper year;
- DB venue/role filters: service-history filters;
- direct researcher-name search: may surface a resolved zero-main-ML researcher.

The person popup shows a Main ML timeline first. **Other publications** is a collapsed secondary section using the presentation publication classes above. Those records are context only.

## 11. Rebuild dependencies

| Change | Required action |
|---|---|
| HTML/CSS/JavaScript only | refresh/restart the website server as needed |
| reviewer service data | rerun entity resolution, publication scan, website build |
| entity-resolution decisions | rerun publication scan, website build |
| pinned DBLP snapshot | rerun entity resolution as appropriate, publication scan, website build |
| display-affiliation cleaning | website build |
| old entity DB missing current/former DBLP affiliation fields | affiliation refresh, website build |
| main-vs-side publication rule | publication scan, website build |
| publication-class presentation rule | website build |

The DBLP publication scan is deliberately separate from `run_website.bat` because it scans the full pinned snapshot.

## 12. Audit outputs

The pipeline intentionally exposes audit artifacts instead of hiding uncertain matches. Current generated audit/report files include:

```text
data/dblp_name_resolution.csv
data/dblp_candidates.csv
data/invalid_reviewer_names.csv
data/reviewer_scraper_repairs.csv
data/reports/unresolved_entity_resolution_candidates.csv
data/reports/unresolved_entity_resolution_audit.json
data/reports/unresolved_entity_resolution_summary.txt
data/reports/high_precision_entity_resolution_plan.csv
data/reports/high_precision_entity_resolution_plan.json
data/reports/affiliation_cleanup_report.csv
data/reports/affiliation_cleanup_report.json
```

These files are derived outputs. They are useful for review but are not required to serve the website once the required upstream data is present.
