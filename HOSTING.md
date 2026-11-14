# Static hosting

Publish the contents of `website/deploy/` at the bucket's site root, keeping their paths:

- `index.html`, `people.html`, `all.html`
- `assets/` (complete folder)
- `data/` (complete folder)

This is the website folder. The pipeline updates its browser data directly. No exclusions needed.

Entry page: `index.html`. No backend required.

Automation: [S3 deployment workflow](.github/workflows/deploy-s3.yml).
