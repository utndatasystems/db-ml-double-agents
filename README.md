# DB × ML Double Agents

Exploring how DB-focused database conference committee members publish at **NeurIPS, ICML and ICLR**.

See [who qualifies](docs/PIPELINE.md#who-qualifies) for the study population and [website structure](website/README.md) for the pages and features.

## Repository map

| Path | Purpose |
| --- | --- |
| `run_pipeline.py`, `scripts/` | Data refresh, matching, classification and website builders; see [pipeline stages](docs/PIPELINE.md#processing-order) |
| `run_website.py`, `website/` | Local server, frontend and generated website files; see [website layout](website/README.md#folder-layout) |
| `data/` | Source downloads, reviewed decisions, working data and audit evidence; see [data guide](data/README.md) |
| `tests/` | Python regressions, browser suites and loading measurements; see [checks](#development) |
| `docs/PIPELINE.md` | Pipeline commands, processing order and counting rules |
| `.github/workflows/` | Static-site validation and S3 deployment; see [hosting](HOSTING.md) |
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
