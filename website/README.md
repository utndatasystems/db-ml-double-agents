# Website

The website has one set of HTML, assets and browser data in the [hosting directory](../HOSTING.md). Three pages share navigation, styling and Methodology:

| Page | Content |
| --- | --- |
| `index.html` | Home: headline findings and trends |
| `people.html` | People Explorer: search, filters and researcher profiles |
| `all.html` | All Insights: charts, classification and coverage Sankeys |

For setup and local viewing, see [Run locally](../README.md#run-locally). HTML/CSS/JavaScript edits do not require a pipeline refresh. Data or calculation changes should go through [the pipeline](../docs/PIPELINE.md).

## Folder layout

- `deploy/`: the single copy of the website.
- `build/`: private build exports; see [details](#private-build-exports).

## Where to edit

| Files in `deploy/assets/` | Responsibility |
| --- | --- |
| `styles.css`, `app.js` | Shared layout, explorer, profiles, analysis charts and Methodology |
| `home.css`, `home.js` | Home layout and rendering |
| `home-trends.js` | Pooled paper shares, endpoint growth and exponential author-growth fit |
| `home-methodology.js` | Load the shared Methodology dialog on demand |
| `db-venue-overlap-model.js`, `db-venue-overlap.js`, `db-venue-overlap.css` | Committee membership calculations and views |
| `population-focus.js` | Classification breakdown and drilldowns |
| `coverage-sankey.js`, `data-coverage.js`, `data-coverage.css` | Coverage flows, record inspection and CSV downloads |
| `loading-preview.js` | Decorative loading previews |
| `favicon.svg` | Browser tab icon |

Keep the shared navigation consistent across all three HTML files. Update asset version query strings when changing cached CSS/JS. The preview helper runs in the document head before the body paints; moving it later can cause chart size jumps.

## Data and interaction

- People Explorer defaults to DB-focused members with an ML paper. All/search can also show matched PC members outside that cohort, with an explanatory badge. Explorer filters do not change the global charts.
- Chart drilldowns link to People Explorer with the selection in the URL. Old `all.html#explore` links redirect there.
- Coverage nodes and ribbons open stage-specific records and CSV exports. See [coverage interpretation](../data/README.md#coverage-interpretation) for counting units and limits.
- Info panels support hover, keyboard focus and click/tap. Dialogs prevent background scrolling. Loading previews must stay inert and preserve the real layout.

For source corrections, see [reviewed decisions](../data/README.md#reviewed-identity-and-paper-decisions). Counting definitions are in the website's Methodology.

## Generated data

The [pipeline](../docs/PIPELINE.md#run-it) generates these files under `deploy/data/`. Do not hand-edit them.

| Files | Purpose |
| --- | --- |
| `site_data.js` | Study-cohort data plus separate matched PC profiles for Explorer |
| `home_summary.json` | Home counts and trend inputs |
| `matrix_stats.js` | DB-venue/ML-venue crossover statistics |
| `people/*.json` | On-demand bibliographies and identity/affiliation evidence |
| `publication_search.<hash>.json` | Broader publication-title index |
| `coverage_summary.json`, `coverage/*.json` | Coverage flows, record lists and source details |

### Private build exports

`build/site_data.json` holds the full study-cohort bibliography used by the matrix builder. `build/matrix_stats.json` is its readable statistics export. The folder is generated, ignored by Git and outside the published directory.

## Loading

Home loads its summary first; shared app and methodology data load when Methodology is requested. People Explorer and All Insights load the researcher index; Insights also loads matrix statistics. Bibliographies, evidence, broader-title search and coverage lists load separately when needed.

The browser export keeps profiles outside the study cohort separate from its `people` array. The app expands shared service records before use. Content-hashed files are immutable; builders retain one previous generation for open pages, so do not manually remove individual detail files.

## Check changes

From the repository root (Chrome or Chromium required):

```sh
python scripts/validate_static_site.py
python tests/run_browser_checks.py
python tests/measure_static_loading.py --profile all --runs 3
```

File/data validation also runs automatically in the pipeline and before deployment. The browser runner checks trend calculations, overlap, classification, navigation, chart interactions, review provenance and loading layouts. The measurement tool compares cold local, throttled and compressed loads, writing `data/reports/static_loading.json`. Ready means real data is displayed, not a loading preview; it does not measure later profile/search interactions. Simulated results are not deployed-host timings.

To measure a deployed host, use `--url https://YOUR-HOST/ --profile network` with the measurement command. Include any hosting path prefix. Both tools accept `--browser PATH`.

## Deploy

See [static hosting](../HOSTING.md).
