# Contributing

LLM Reviewer is intentionally small. Good contributions keep the runtime simple,
generic, and safe to run against private repositories.

## Ground Rules

- Do not commit secrets, private repository names, customer names, tokens, or
  screenshots that expose private code.
- Keep changes generic. Repo-specific review rules belong in examples or local
  config, not in core code.
- Keep pull requests focused. Separate docs, packaging, prompt, and runtime
  behavior changes when possible.
- Add or update tests with code changes.
- Prefer plain Python, shell wrappers, and existing dependencies over adding a
  framework.

## Development

```sh
uv sync --dev
uv run pre-commit install   # one-time: installs both pre-commit and commit-msg hooks
uv run pytest -q
uv run python -m compileall -q src tests
```

Local secrets live in ignored `config/env.toml`.

## Commit messages — Conventional Commits

Commit messages MUST follow [Conventional Commits 1.0][cc]. The format is
enforced two ways: a `commit-msg` pre-commit hook on your machine, and the
`Conventional Commits` GitHub Action on every PR. A non-conforming commit
will be rejected before it can be pushed or merged.

[cc]: https://www.conventionalcommits.org/en/v1.0.0/

```
<type>(<scope>): <subject>

[optional body]

[optional footers, including BREAKING CHANGE: ...]
```

| Type | When to use it | Triggers SemVer bump? |
|---|---|---|
| `feat` | A user-visible feature, config flag, or new CLI behavior | **minor** |
| `fix` | A bug fix that an end user could observe | **patch** |
| `perf` | A change purely to performance | **patch** |
| `refactor` | An internal change with no behavior change | — |
| `docs` | Documentation only (README, CHANGELOG, docstrings) | — |
| `test` | Adding or fixing tests, no source change | — |
| `build` | Build system, packaging, dependencies | — |
| `ci` | CI workflows, hooks, repo automation | — |
| `chore` | Repo housekeeping that isn't any of the above | — |
| `style` | Formatting / whitespace only | — |

A footer line `BREAKING CHANGE: <description>` (or an `!` after the type,
e.g. `feat!: …`) triggers a **major** bump — except while the project is
pre-1.0, where it triggers a **minor** bump per the `major_version_zero`
SemVer carve-out in `pyproject.toml`.

Examples:

```
feat(scm): add GitHub pull-request support
fix(poller): honor BUBO_PROVIDER env override in load_review_config
docs(security): expand SECURITY.md with disclosure timelines
chore(deps): bump ruff to 0.14.7
feat(review)!: drop manual_review_dry_run alias

BREAKING CHANGE: `manual_review_dry_run` was removed; use `[agents].dry_run`.
```

### Cutting a release

Releases are automated by [release-please](https://github.com/googleapis/release-please).
On every push to `main`, the `Release Please` workflow either opens a new
release PR or updates the existing one with the next computed version
from Conventional Commits since the last tag:

| Commit type | Bump |
|---|---|
| `fix:` | patch |
| `feat:` | minor |
| `feat!:` / `BREAKING CHANGE:` footer | minor (pre-1.0 carve-out via `bump-minor-pre-major`) |
| `chore:` / `docs:` / `test:` / `ci:` / `build:` / `style:` / `refactor:` | no bump |

The release PR bumps `pyproject.toml` and updates
`.release-please-manifest.json`. **Before merging, rename the
`## [Unreleased]` block in `CHANGELOG.md` to `## [X.Y.Z] - YYYY-MM-DD`**
and add an empty `## [Unreleased]` above it — release-please intentionally
does not touch the hand-curated CHANGELOG (its own auto-generated changelog
lives in `.release-please-changelog.md`, which exists only as a sink).

Merging the release PR creates a `vX.Y.Z` tag, which fires
`.github/workflows/release.yml` (build sdist + wheel, generate SBOM,
cosign-sign, publish the GitHub Release with artifacts).

#### One-time setup (for repo admins)

The workflow requires a `RELEASE_PAT` secret — a fine-grained Personal
Access Token (or GitHub App installation token) with `contents: write`
and `pull-requests: write` on this repo. The default `GITHUB_TOKEN`
cannot trigger downstream workflows on tag push by design, so without
this token the tag push would not fire `release.yml`.

```sh
# Generate a fine-grained PAT at https://github.com/settings/tokens
# scoped to this repo with the two permissions above, then:
gh secret set RELEASE_PAT --body "<your-pat>"
```

#### Cz still validates commit messages

`commitizen` remains the source of truth for Conventional Commits
format enforcement (`pre-commit` `commit-msg` hook locally;
`.github/workflows/commitlint.yml` on every PR). It just no longer
performs the version bump — release-please does.

## Pull Requests

Before opening a PR:

```sh
uv run pytest -q
uv run python -m compileall -q src tests
git diff --check
```

Include the command output or a short verification note in the PR description.

## Useful Areas

- Better line-position mapping for GitLab discussions.
- More SCM adapters while keeping GitLab first.
- More telemetry rollups and dashboards.
- Safer prompt-output parsing.
- Packaging and install docs for new platforms.
