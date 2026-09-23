# Generated website data

`run_website.bat` writes:

```text
site_data.json
site_data.js
matrix_stats.json
matrix_stats.js
```

`site_data.*` is built from `data/entity_resolution.sqlite` and `data/dblp_publications_for_db_people.csv`.

`matrix_stats.*` is then built from the resolved DB-service data plus `site_data.json` and counts only exact main NeurIPS/ICML/ICLR papers.

Do not hand-edit these files. Change the upstream data or builder code and regenerate them.
