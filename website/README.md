# Website

`website/` is a static front end. `run_website.bat` rebuilds its browser data when the local entity-resolution database is available, then serves this directory with Python's HTTP server.

## Generated browser data

```text
website/data/site_data.json
website/data/site_data.js
website/data/matrix_stats.json
website/data/matrix_stats.js
```

The `.js` files wrap the same payloads for direct browser loading. Do not hand-edit these generated files.

## Main page

The main page is deliberately focused on exact main-conference NeurIPS/ICML/ICLR activity by resolved DB researchers.

- **Crossovers** — read-only all-years DB venue × main-ML venue matrix.
- **Publication activity** — main NeurIPS/ICML/ICLR papers only.
- **Research explorer** — DB researchers with main-ML activity by default.

An explicit researcher-name search can surface a resolved DB researcher with zero main-ML papers. This is only a discovery exception; ML venue/year filters still require main-ML activity.

## Explorer filters

Current explorer controls are:

- researcher search;
- DB venue;
- ML venue;
- ML publication year;
- DB role;
- sort;
- reset filters.

DB venue/role are service-history filters. ML venue/year refer only to exact main NeurIPS/ICML/ICLR papers.

When a specific ML venue is selected, the front end uses UTN green as the selection color and mutes the other ML venues for context.

## Person popup

The popup keeps the core signal first:

1. Main ML timeline and publication list;
2. collapsed **Other publications** section.

The secondary section uses the descriptive DBLP publication classes:

```text
Database
Other conference
Journal
Workshop
Preprint
Other
```

These records are context only and do not count as DB × ML crossover evidence.

## Affiliations

The explorer row prefers DBLP's current affiliation when current/former metadata is available in `entity_resolution.sqlite`. If current DBLP affiliation is unavailable, the site-data builder falls back to the latest observed DB-service affiliation.

The popup keeps the broader affiliation history.

## Editing

```text
HTML        website/index.html
CSS         website/assets/styles.css
JavaScript  website/assets/app.js
```

Visual-only edits do not require another DBLP publication scan. `run_website.bat` can be used to rebuild/serve the browser payloads after source-data changes.
