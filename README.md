![From SIGMOD to NeurIPS: Are database researchers switching sides?](docs/assets/readme-header.png)

# DB × ML Double Agents

[![Website](https://img.shields.io/badge/Website-%F0%9F%8C%90-00a86b)](https://utndatasystems.github.io/db-ml-double-agents/)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Data updated](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Futndatasystems%2Fdb-ml-double-agents%2Fmain%2Fdata%2Frefresh_badge.json)](docs/PIPELINE.md#run-it)
[![Refresh datasets](https://github.com/utndatasystems/db-ml-double-agents/actions/workflows/refresh-datasets.yml/badge.svg)](https://github.com/utndatasystems/db-ml-double-agents/actions/workflows/refresh-datasets.yml)
[![Deploy website](https://github.com/utndatasystems/db-ml-double-agents/actions/workflows/deploy-pages.yml/badge.svg)](https://github.com/utndatasystems/db-ml-double-agents/actions/workflows/deploy-pages.yml)

Machine learning is becoming an increasingly important part of database research, but are we simply adopting ML techniques, or are we slowly becoming ML researchers ourselves? We take a look at publication trends across major database and ML conferences to see how the two communities are evolving and who might be taking over whom.

**[Explore the website](https://utndatasystems.github.io/db-ml-double-agents/)**

The study follows DB-focused members of database conference committees and their publications at **NeurIPS, ICML and ICLR**. Explore individual researchers and publication trends, and follow the data coverage and methodology behind the results. See [who qualifies](docs/PIPELINE.md#who-qualifies) for the study population and [website structure](website/README.md) for the pages and features.

The datasets and website are refreshed weekly. See [automation and hosting](HOSTING.md) for the schedule, manual runs and deployment setup.

## Repository map

| Path | Purpose |
| --- | --- |
| `run_pipeline.py`, `scripts/` | Data refresh, matching, classification and website builders; see [pipeline stages](docs/PIPELINE.md#processing-order) |
| `run_website.py`, `website/` | Local server, frontend and generated website files; see [website layout](website/README.md#folder-layout) |
| `data/` | Source downloads, reviewed decisions, working data and audit evidence; see [data guide](data/README.md) |
| `tests/` | Python regressions, browser suites and loading measurements; see [checks](#development) |
| `docs/PIPELINE.md` | Pipeline commands, processing order and counting rules |
| `.github/workflows/` | Weekly dataset refresh and static-site deployment; see [automation and hosting](HOSTING.md) |
| `requirements.txt`, `.gitignore` | Python dependencies and files kept outside Git |

`.venv/` and `__pycache__/` are generated local files. `.git/` holds version history.

## Run locally

Select your operating system for one-time setup:

<details>
<summary>Windows (PowerShell)</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

</details>

<details>
<summary>macOS</summary>

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

</details>

<details>
<summary>Linux</summary>

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

</details>

<br>

View the existing website at **http://localhost:8000**:

```sh
python run_website.py
```

For data refreshes and rebuilds, see [pipeline commands](docs/PIPELINE.md#run-it).

## Deploy

See [static hosting](HOSTING.md).

## Development

```sh
python -m unittest discover -s tests -v
```

For browser checks and loading measurements, see [frontend checks](website/README.md#check-changes).

## Reviewed decisions

See [review files and guidance](data/README.md#reviewed-identity-and-paper-decisions) for manual and agent-assisted decisions.
