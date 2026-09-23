# Bubo documentation

The documentation site is built with Nextra and published to GitHub Pages by
the repository's `.github/workflows/deploy-docs.yml` workflow.

## Local development

From this directory, install the lockfile-pinned dependencies and run the site:

```sh
npm ci
npm run dev
```

Visit <http://localhost:3000>. Run `npm run build` to create the static site
in `out/`, matching the deployment workflow.
# Operator UI audit detail

`bubo ui-export` keeps report aggregates over the full selected window. To keep
the static export responsive, each report includes only the newest 1,000 audit
rows and exposes `audit_total` plus `audit_truncated`; the Reports view shows
this disclosure when detail is truncated.
