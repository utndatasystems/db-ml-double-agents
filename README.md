# DB ↔ ML Crossovers

A small static explorer for researchers who participate in major database-systems conference service and also publish at major machine-learning venues.

The repository has two parts:

- a Python data pipeline that collects and enriches reviewer and paper metadata; and
- a dependency-free static website that reads one generated dataset.

The public website is deliberately results-focused. Identity-resolution and data-processing details stay in the pipeline rather than being shown in the main UI.

## Repository layout

```text
ml-db-paper-traitor-website/
├─ .venv/                         # local Python environment; gitignored
├─ data/
│  ├─ *.sqlite                    # local working databases; gitignored
│  ├─ dblp_snapshot/              # ~1 GB DBLP snapshot; gitignored
│  ├─ *.csv                       # derived review/export files
│  └─ *.txt                       # derived summaries
├─ scripts/
│  ├─ scrape_*                    # conference collectors
│  ├─ enrich_reviewers_with_dblp.py
│  ├─ enrich_ml_with_dblp.py
│  ├─ summarize_db_ml_entity_resolution.py
│  └─ build_site_data.py
├─ website/
│  ├─ index.html
│  ├─ assets/
│  │  ├─ app.js
│  │  └─ styles.css
│  └─ data/
│     ├─ site_data.js             # generated + committed for static hosting
│     └─ site_data.json           # same data in reusable JSON form
├─ requirements.txt
├─ run_website.bat
└─ README.md
```

`website/data/site_data.js` and `website/data/site_data.json` are generated files. Do not hand-edit them; rebuild them from the current enrichment databases.

## Windows setup

From the repository root in PowerShell:

```powershell
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r .\requirements.txt
```

All commands below assume they are run from the repository root.

## Run the website

```powershell
.\run_website.bat
```

Then open:

```text
http://localhost:8000
```

If `data/entity_resolution.sqlite` and `data/ml_entity_resolution.sqlite` exist locally, the launcher first rebuilds the website dataset. If the local SQLite files are absent, for example in a fresh clone, it serves the committed `website/data/site_data.js` instead.

To rebuild website data without starting the server:

```powershell
python .\scripts\build_site_data.py
```

## Data pipeline

The scripts default to the repository's `data/` directory, so explicit `--db` or `--data-dir` arguments are optional unless you want another location.

### 1. Collect database-conference reviewer/service records

```powershell
python .\scripts\scrape_sigmod_reviewers.py --years 2022 2023 2024 2025 2026
python .\scripts\scrape_vldb_reviewers.py   --years 2024 2025 2026
python .\scripts\scrape_cidr_reviewers.py   --years 2023 2024 2025 2026
python .\scripts\scrape_icde_reviewers.py   --years 2024 2025 2026
python .\scripts\scrape_edbt_reviewers.py   --years 2019 2020 2021
```

Default outputs:

```text
data/sigmod_reviewers.sqlite
data/vldb_reviewers.sqlite
data/cidr_reviewers.sqlite
data/icde_reviewers.sqlite
data/edbt_reviewers.sqlite
```

The EDBT scraper includes the corrected PDF-column parsing used for the 2019–2021 committee lists.

### 2. Collect ML papers and authors

```powershell
python .\scripts\scrape_neurips_papers.py --years 2022 2023 2024 2025
python .\scripts\scrape_icml_papers.py     --years 2022 2023 2024 2025
python .\scripts\scrape_iclr_papers.py     --years 2024 2025 2026
```

Default outputs:

```text
data/neurips_papers.sqlite
data/icml_papers.sqlite
data/iclr_papers.sqlite
```

### 3. Enrich DB reviewers with DBLP identities

```powershell
python .\scripts\enrich_reviewers_with_dblp.py
```

On the first run this downloads the pinned DBLP monthly XML snapshot into `data/dblp_snapshot/`. The snapshot is intentionally gitignored and reused by later runs.

Important outputs include:

```text
data/entity_resolution.sqlite
data/dblp_name_resolution.csv
data/dblp_candidates.csv
data/invalid_reviewer_names.csv
```

### 4. Enrich ML paper authors with DBLP identities

```powershell
python .\scripts\enrich_ml_with_dblp.py
```

This reuses the local DBLP snapshot and matches ML papers to DBLP publication records before resolving their author signatures.

Important outputs include:

```text
data/ml_entity_resolution.sqlite
data/ml_dblp_paper_resolution.csv
data/ml_dblp_author_occurrences.csv
data/ml_dblp_people.csv
data/db_ml_dblp_pid_matches.csv
```

### 5. Build the one-row-per-person DB ↔ ML overlap summary

```powershell
python .\scripts\summarize_db_ml_entity_resolution.py
```

The crossover result is based on the same persistent DBLP PID appearing on the database-service side and the ML-publication side.

Important outputs:

```text
data/db_ml_resolved_people.csv
data/db_ml_entity_resolution_summary.txt
```

### 6. Regenerate the static website dataset

```powershell
python .\scripts\build_site_data.py
```

This writes:

```text
website/data/site_data.json
website/data/site_data.js
```

The browser only consumes this generated, person-centric dataset; it does not read SQLite directly.

## Current website views

The initial frontend is intentionally small and easy to extend:

- summary cards;
- DB venue × ML venue crossover matrix;
- researcher search/filter/sort table; and
- researcher detail dialog with DB service history and ML publications.

The frontend uses plain HTML, CSS, and JavaScript. There is no framework, build tool, API server, or npm dependency.

## Extending the project

New data should normally be added in this order:

1. extend the SQLite/enrichment pipeline;
2. expose the new fields in `scripts/build_site_data.py`; and
3. render the fields in `website/assets/app.js`.

That keeps raw-source quirks and identity logic out of the frontend.

Natural future extensions include topic tags, coauthor summaries, additional venues, richer affiliation history, citation metadata, and manually resolved ambiguous identities.

## Git / large files

The following are intentionally not committed:

```text
.venv/
data/*.sqlite
data/dblp_snapshot/
```

The generated static website dataset **is** committed so that the website can be hosted or previewed without the local SQLite databases or the 1 GB DBLP snapshot.
