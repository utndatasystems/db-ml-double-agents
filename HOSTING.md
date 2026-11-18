# Automation and static hosting

## Weekly dataset refresh

[Refresh datasets](.github/workflows/refresh-datasets.yml) runs every Monday at
03:17 UTC (04:17 Berlin in winter, 05:17 in summer), and can also be started
manually. Scheduled start times can be delayed by GitHub. It checks out `main`,
installs Python 3.12, Node.js 24 and all packages in `requirements.txt` into an
isolated Python environment, then runs:

```sh
python run_pipeline.py --full-refresh
```

The pipeline includes static-site validation, Python tests and a JavaScript
syntax check. No browser installation is needed for these checks. The job has
a three-hour timeout to allow for the expected one-hour run and slower downloads.

After success, the workflow commits additions, changes and deletions under
`data/` and `website/deploy/` directly to `main`, using `github-actions[bot]`.
Ignored snapshots, SQLite databases, reports and build exports remain untracked.
There is no PR, and no commit if nothing changed. Before pushing, it rebases onto
the latest `main`; a conflict fails the job rather than force-pushing.

One-time setup:

1. Push these workflow files to `main`.
2. Under **Settings → Actions → Runners**, confirm the self-hosted runner is
   online with labels `self-hosted`, `Linux`, `X64`. These match the existing
   `watzmann-db-ml-double-agents` runner. If several runners match, add a custom
   label to the intended runner and to `runs-on` in the workflow.
3. Keep the runner software current (at least v2.327.1 for the Node 24 actions).
   It needs Bash, Git, outbound HTTPS access, a writable Python tool cache and
   sufficient disk/RAM for the full DBLP/ROR pipeline. Python and Node are
   installed by the setup actions; Python packages are installed on every run.
   For Linux distributions other than supported Ubuntu versions, consult
   [setup-python's self-hosted requirements](https://github.com/actions/setup-python/blob/main/docs/advanced-usage.md#using-setup-python-with-a-self-hosted-runner).
4. Organization Actions policies must allow the actions and the workflow's
   `contents: write` permission. Branch protection/rulesets on `main` must allow
   this automation to push directly. A rule requiring every change to arrive
   through a PR will otherwise reject the final push. No PAT is required.

The checkout preserves ignored working files so the runner can reuse large
snapshots and caches. Use a dedicated checkout for this job. The weekly and
manual refreshes share a concurrency group and will not run simultaneously.

### Protect the self-hosted runner

The refresh job accepts only `schedule` or `workflow_dispatch` events in this
repository on `main`, and checks out `main`. Keep PR checks on GitHub-hosted
runners. These YAML guards are defense in depth: a contributor can propose a
new workflow or edit the guards in a PR, so runner access also needs a policy
outside the proposed code.

Under **Settings → Actions → General → Approval for running fork pull request
workflows from contributors**, select **Require approval for all outside
collaborators**. This repository's API value was set and verified as
`all_external_contributors`. Do not approve a fork workflow that sends code to
the server; approval permits its proposed code to execute.

For a GitHub-enforced workflow restriction, an organization owner should:

1. Register the runner at organization level if it is currently registered
   directly to this repository; repository runners cannot use organization
   runner-group policies.
2. Under **Organization Settings → Actions → Runner groups**, assign it a
   dedicated group and select only `utndatasystems/db-ml-double-agents` for
   repository access. Public repository access must be enabled for this repo.
3. Where **Workflow access → Selected workflows** is available on the plan,
   allow only:

   ```text
   utndatasystems/db-ml-double-agents/.github/workflows/refresh-datasets.yml@refs/heads/main
   ```

   Only jobs defined in that workflow at that ref may use the group. PR refs
   and newly added workflows are excluded. Runner labels alone do not enforce
   this restriction.
4. Protect `main` with trusted review for workflow and executable-code changes,
   while retaining the refresh bot's narrowly controlled direct-push access.

See [GitHub's runner-group workflow restrictions](https://docs.github.com/en/enterprise-cloud@latest/actions/how-tos/manage-runners/self-hosted-runners/manage-access#changing-which-workflows-can-access-a-runner-group).
If selected-workflow restrictions are unavailable, put the automation and runner
in a private repository writable only by trusted maintainers; keep the public
repository's workflows on GitHub-hosted runners. This requires separate setup
for publishing generated data back to the public repository.

Even trusted runs execute dependencies and repository code. Run the agent in
an isolated VM with a dedicated unprivileged account and limited network access,
without unrelated server credentials or a host Docker socket. Fork approval is
an approval gate, not isolation of code after it is approved.

### Test a refresh manually

1. In the repository's **Actions** tab, select **Refresh datasets**.
2. Click **Run workflow**, select branch **main**, then **Run workflow**.
3. Open the run and follow the install, pipeline and commit steps. This is a
   real full refresh: allow about an hour, and expect a direct commit on success.
4. Confirm the bot commit appears on `main` and contains the generated files.
   If there are no changes, the commit step reports that instead.
5. A successful refresh starts **Deploy website to GitHub Pages** automatically.
   Check that separate run for the deployment link.

With GitHub CLI authenticated, the equivalent is:

```sh
gh workflow run refresh-datasets.yml --ref main
gh run list --workflow refresh-datasets.yml --limit 5
gh run watch RUN_ID
```

For a local command preview without downloading sources or committing anything:

```sh
python run_pipeline.py --full-refresh --dry-run
```

## GitHub Pages

The project site URL is:
**https://utndatasystems.github.io/db-ml-double-agents/**

GitHub automatically serves an organization's project repositories under
`https://<organization>.github.io/<repository>/`. The main lab website can stay
in the separate `utndatasystems.github.io` repository. No files need to be
copied there; adding a link from the main website is optional. See
[GitHub's site types](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages#types-of-github-pages-sites).

One-time setup:

1. In this repository, go to **Settings → Pages**.
2. Under **Build and deployment → Source**, select **GitHub Actions**.
3. Leave **Custom domain** empty; `utndatasystems.github.io` is the built-in
   organization domain, and the repository name supplies the path.
4. After the workflows are on `main`, go to **Actions → Deploy website to
   GitHub Pages → Run workflow**, select `main`, and run it. This publishes the
   existing dataset without waiting for a refresh.
5. Open the URL from the successful deployment. Check Home, People Explorer,
   All Insights and a researcher profile to exercise lazy-loaded data.

The [Pages workflow](.github/workflows/deploy-pages.yml) runs on relevant pushes
to `main`, manual requests, and successful **Refresh datasets** completions.
The completion trigger is necessary because pushes made using `GITHUB_TOKEN`
[do not start push workflows](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).
It checks out the latest `main` so the refresh's new commit is included.
Deployments are serialized. Authentication uses the built-in token and OIDC;
no deployment secrets are needed. The deployment uses the `github-pages`
environment; any required reviewers configured there will pause deployment.

The website is static and uses relative asset/data links, so the repository
path requires no frontend build or base-URL setting. Only the published files
are uploaded, not the Python pipeline or private build exports.

### Site size and deployment preparation

GitHub Pages [limits published sites to 1 GB](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits).
The repository's browser data retains one previous generation and currently
exceeds that size. [prepare_github_pages.py](scripts/prepare_github_pages.py)
creates a separate deployment copy containing only the currently referenced
detail/search/coverage data, leaving repository files intact. It follows nested
data references, validates both the source and staged website, and fails before
upload if the current data alone reaches 1 GB. Readers holding older pages may
need to reload after deployment because older data URLs are not published.

To validate and prepare locally (use a new output directory each time):

```sh
python scripts/prepare_github_pages.py --output /tmp/db-ml-pages-preview
```
