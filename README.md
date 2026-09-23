# Database × Machine Learning Explorer

A public-facing website for exploring **database-community researchers who also publish at the main NeurIPS, ICML, or ICLR conferences**.

The project combines database-community service data with DBLP publication data using a conservative, identity-first pipeline.

## What the project measures

A researcher counts as a **DB × ML crossover** only when both of the following belong to the **same resolved DBLP person ID**:

1. the person appears in database-community service data from **CIDR, EDBT, ICDE, SIGMOD, or VLDB**;
2. the person has at least one exact **main-conference** publication at **NeurIPS/NIPS, ICML, or ICLR** in DBLP.

Other DBLP publications are retained so a researcher's broader publication history can be inspected, but they do **not** count as crossover evidence.

The pipeline does not maintain a separate ML-paper scraper or a separate ML-side person-resolution system. Once a database researcher has been resolved to a DBLP PID, DBLP is the canonical source for that person's publication history.

---

## Pipeline overview

The workflow is:

```text
DB conference/service pages
        │
        ▼
scraped DB service records
        │
        ▼
DBLP person resolution
        │
        ▼
resolved DBLP PIDs
        │
        ▼
DBLP publication scan
        │
        ▼
publication corpus
        │
        ▼
static website data
        │
        ▼
website
```

The important principle is that **identity is resolved before publication history is collected**.

This avoids independently matching the same person across multiple ML data sources.

---

## Core repository layout

```text
.
├─ scripts/
│  ├─ scrape_cidr_reviewers.py
│  ├─ scrape_edbt_reviewers.py
│  ├─ scrape_icde_reviewers.py
│  ├─ scrape_sigmod_reviewers.py
│  ├─ scrape_vldb_reviewers.py
│  ├─ scraper_repairs.py
│  │
│  ├─ enrich_reviewers_with_dblp.py
│  ├─ audit_unresolved_entity_resolution.py
│  ├─ apply_high_precision_entity_resolution.py
│  │
│  ├─ scan_dblp_publications.py
│  ├─ build_site_data.py
│  ├─ build_matrix_stats.py
│  └─ display_cleaning.py
│
├─ data/
│  ├─ README.md
│  └─ dblp_snapshot/
│
├─ docs/
│  └─ PIPELINE.md
│
├─ website/
│  ├─ index.html
│  ├─ README.md
│  ├─ assets/
│  │  ├─ app.js
│  │  └─ styles.css
│  └─ data/
│
├─ run_entity_resolution_audit.bat
├─ run_high_precision_entity_resolution.bat
├─ apply_high_precision_entity_resolution.bat
├─ run_dblp_publication_scan.bat
├─ run_website.bat
└─ requirements.txt
```

### What the main parts do

**`scripts/scrape_*_reviewers.py`**
Collect database-community service records from the supported conference/service pages.

**`scripts/scraper_repairs.py`**
Contains explicit source-specific cleanup rules for known scraper irregularities. These repairs are applied during downstream processing rather than modifying the raw scraped records.

**`scripts/enrich_reviewers_with_dblp.py`**
Builds the DB researcher identity layer. It matches scraped researcher names conservatively to DBLP people and stores resolved DBLP PIDs together with relevant DBLP person metadata.

**`scripts/audit_unresolved_entity_resolution.py`**
Produces an audit queue for names that could not be resolved automatically.

**`scripts/apply_high_precision_entity_resolution.py`**
Applies only the additional matches that satisfy the project's guarded high-precision rules.

**`scripts/scan_dblp_publications.py`**
Scans the pinned DBLP snapshot for publications belonging to the already-resolved researchers.

**`scripts/build_site_data.py`**
Transforms resolved people, service history, affiliations, and publications into the browser data used by the website.

**`scripts/build_matrix_stats.py`**
Builds the DB-venue × ML-venue crossover statistics shown on the site.

**`website/`**
Contains the static front end. No backend is required once the generated website data exists.

---

# Setup

The project is intended to be run from Windows PowerShell.

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

# Run the complete pipeline from scratch

A fresh run does **not** require an existing `entity_resolution.sqlite`.

## 1. Scrape database-community service

Run the five database venue scrapers:

```powershell
python .\scripts\scrape_cidr_reviewers.py
python .\scripts\scrape_edbt_reviewers.py
python .\scripts\scrape_icde_reviewers.py
python .\scripts\scrape_sigmod_reviewers.py
python .\scripts\scrape_vldb_reviewers.py
```

These produce venue-specific SQLite databases under:

```text
data/
```

They contain the observed database-community service records used as the starting population for the project.

When no explicit year range is supplied, the scrapers use their configured default range.

---

## 2. Resolve database researchers to DBLP people

Run:

```powershell
python .\scripts\enrich_reviewers_with_dblp.py
```

This creates the central identity database:

```text
data/entity_resolution.sqlite
```

It also creates CSV outputs that make the resolution process auditable.

### DBLP snapshot

The resolver uses the pinned DBLP snapshot:

```text
data/dblp_snapshot/dblp-2026-09-01.xml.gz
data/dblp_snapshot/dblp-2023-06-28.dtd
```

If the pinned files are not present locally, the pipeline can obtain and checksum-verify them.

The same snapshot is reused later for the publication scan.

### Resolution policy

Automatic matching is intentionally conservative.

A DBLP PID is assigned automatically only when the resolver has sufficiently strong identity evidence. Ambiguous names and weaker candidates remain unresolved rather than being guessed.

The resulting entity database also stores DBLP person metadata used by the site, including person aliases, verified identifiers where available, and DBLP affiliation information.

---

## 3. Audit unresolved identities

Generate the unresolved-person audit:

```powershell
.\run_entity_resolution_audit.bat
```

This reports the remaining unresolved names and the available DBLP candidates.

For the additional guarded high-precision pass, first generate a dry-run plan:

```powershell
.\run_high_precision_entity_resolution.bat
```

Review the generated plan before applying it.

Then run:

```powershell
.\apply_high_precision_entity_resolution.bat
.\run_entity_resolution_audit.bat
```

The apply step changes only matches accepted by the high-precision rules.

Researchers that remain ambiguous should remain unresolved unless they are reviewed separately.

---

## 4. Collect DBLP publications

Once the researcher identities are resolved, run:

```powershell
.\run_dblp_publication_scan.bat
```

The scanner reads the same pinned DBLP snapshot and collects paper-like DBLP publications for the resolved researchers.

The resulting publication corpus is:

```text
data/dblp_publications_for_db_people.csv
```

### Publication-author identity

Publication matching remains strict.

The scanner can associate a publication with a resolved researcher using DBLP identity evidence such as:

1. a direct DBLP author PID;
2. a verified ORCID associated with the resolved DBLP person;
3. an exact case-sensitive DBLP author signature that belongs uniquely to that person.

It does **not** use fuzzy author-name matching to attach publications to people.

---

## 5. Build and run the website

Run:

```powershell
.\run_website.bat
```

This rebuilds the generated website data from:

```text
data/entity_resolution.sqlite
data/dblp_publications_for_db_people.csv
```

and writes browser-ready data under:

```text
website/data/
```

It then serves the site locally at:

```text
http://localhost:8000
```

The DBLP publication scan is **not** performed during normal website startup.

---

# Publication semantics

The publication corpus distinguishes two fundamental scopes.

## Main ML

`main`

Exact main-conference publications at:

```text
NeurIPS / NIPS
ICML
ICLR
```

Only these publications count toward DB × ML crossover status.

## Context publications

`side`

Other included DBLP paper-like publications belonging to the same resolved researcher.

These publications provide context in the researcher detail view but do not affect crossover membership.

---

## Publication classes used by the website

For presentation, `build_site_data.py` assigns publications to descriptive classes:

```text
main_ml
database
other_conference
journal
workshop
preprint
other
```

These classes are for display only.

They do not change whether someone qualifies as a DB × ML researcher.

Workshop classification is deliberately conservative: DBLP metadata must provide explicit workshop evidence.

CoRR records are treated as preprints unless stronger DBLP metadata identifies another publication context.

---

# Website semantics

The public website focuses on the question:

> **Which database researchers publish at main NeurIPS, ICML, or ICLR venues, and when?**

## Crossovers

A read-only all-years summary showing the overlap between:

```text
DB service venue × main ML publication venue
```

The denominator for each database venue is the set of resolved researchers observed in that database-service community.

## Publication activity

Shows the main NeurIPS, ICML, and ICLR publication activity of the DB researchers in the dataset over time.

Only exact main-ML publications appear in this primary timeline.

## Research explorer

By default, the explorer shows resolved DB researchers with at least one main-ML publication.

Filters include database venue, database role, ML venue, and ML publication year.

A direct researcher-name search can also surface a resolved DB researcher with no main-ML publication. This makes it possible to inspect their database service and broader DBLP publication history without changing the definition of a DB × ML crossover.

## Researcher detail

The person popup separates:

```text
Main ML
```

from:

```text
Other publications
```

The Main ML timeline contains only NeurIPS, ICML, and ICLR main-conference papers.

The collapsed Other publications section contains the broader DBLP publication history for context.

---

# Important data products

| Path                                       | Meaning                                                       |
| ------------------------------------------ | ------------------------------------------------------------- |
| `data/*_reviewers.sqlite`                  | Raw/local database-community service records                  |
| `data/dblp_snapshot/`                      | Pinned DBLP XML snapshot and DTD                              |
| `data/entity_resolution.sqlite`            | DB researcher → DBLP identity state and DBLP person metadata  |
| `data/dblp_name_resolution.csv`            | Human-readable entity-resolution summary                      |
| `data/dblp_candidates.csv`                 | Candidate DBLP identities considered during resolution        |
| `data/dblp_publications_for_db_people.csv` | Publication corpus for resolved DB researchers                |
| `data/reports/`                            | Entity-resolution and cleanup audit outputs                   |
| `website/data/site_data.js`                | Generated researcher/publication data consumed by the browser |
| `website/data/matrix_stats.js`             | Generated crossover matrix data                               |

Most of these are derived and can be rebuilt.

`entity_resolution.sqlite` is also rebuildable, but keeping it avoids repeating expensive identity-resolution work and preserves already-applied reviewed decisions.

---

# When to rebuild

| What changed?                                  | What to run                                          |
| ---------------------------------------------- | ---------------------------------------------------- |
| HTML, CSS, or JavaScript only                  | restart/refresh the website                          |
| DB service scraping results                    | entity resolution → publication scan → website build |
| entity-resolution decisions                    | publication scan → website build                     |
| DBLP snapshot                                  | entity resolution → publication scan → website build |
| publication matching or main-ML classification | publication scan → website build                     |
| website publication/display classification     | website build                                        |
| display-only affiliation cleanup               | website build                                        |

---

# Static hosting

The deployed website does not require Python, SQLite, or a backend.

For static hosting, publish the contents of:

```text
website/
```

including the generated files under:

```text
website/data/
```

The research pipeline and the large DBLP snapshot only need to exist on the machine used to regenerate the data.

---

# Further documentation

For more detail about identity resolution, DBLP matching, publication classification, and generated artifacts:

```text
docs/PIPELINE.md
```

For front-end structure and behavior:

```text
website/README.md
```

For notes about files under the data directory:

```text
data/README.md
```
