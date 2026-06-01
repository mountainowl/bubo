# Changelog

All notable changes to this project are tracked in this file.

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
formatting and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries land here on the same PR that ships the change. The first
production tag (`0.1.0`) cuts everything currently under "Unreleased".

## [Unreleased]

### Added
- **`mcp-llm-reviewer` server.** New MCP server with two interfaces
  exposed to any MCP-capable client (Codex, Claude Desktop, Cline).
  *Metrics interface* (read-only against SQLite): `health`,
  `list_recent_reviews`, `get_review`, `get_findings`,
  `get_finding_outcomes`, `get_metrics` (aggregate counts + token/cost
  sums over a configurable window). *Review interface*: `review_change`
  triggers a one-shot review by URL or
  `(provider={gitlab,github,auto}, project, number)`, blocks until the
  underlying `code-review-codex` subprocess finishes, and returns the
  parsed findings JSON alongside the raw transcript. MCP-triggered
  reviews intentionally do not write to `reviewed_mrs` — metrics reflect
  only poller-driven reviews. Two transports selectable via the new
  `[mcp_server]` config section: `stdio` (default; Codex spawns the
  process per session) and `http` (long-lived HTTP+SSE server bound to
  `host:port`, every request must present `Authorization: Bearer
  <bearer_token>` or get a 401). HTTP auth is a thin ASGI middleware
  with constant-time bearer compare; the server does not terminate TLS
  (front with nginx/caddy or bind only to localhost). Backed by the
  official `mcp` Python SDK (FastMCP). New console script +
  `bin/mcp-llm-reviewer` launcher; new readers in `db.py`
  (`list_recent_reviews`, `get_review_row`, `findings_for`,
  `outcomes_for`, `metrics_summary`).
- **OpenSSF Scorecard hardening pass.** Pinned every GitHub Action by full
  commit SHA across `ci.yml`, `integration.yml`, `release.yml`,
  `scorecard.yml` (with a trailing `# vX.Y.Z` comment so Dependabot keeps
  the SHAs current). Added `.github/workflows/codeql.yml` (Python,
  security-and-quality query set, weekly schedule) so the SAST check
  detects a static analyzer. Added Hypothesis-based property tests
  (`tests/test_property_based.py`) covering env-var interpolation, stable
  hashing, and the findings-extractor — Scorecard's fuzzing detector
  recognizes Hypothesis. Extended `release.yml` to sign every release
  artifact (wheel, sdist, SBOM) with `cosign` via Sigstore keyless OIDC,
  attaching the `.sig` + `.pem` files alongside the artifacts. Fleshed out
  `SECURITY.md` with explicit response timelines, in-/out-of-scope
  vulnerability types, and a coordinated-disclosure clause. Enabled branch
  protection on `main`: 1 required approval, stale-review dismissal,
  enforce-admins, required linear history, required conversation
  resolution, no force-pushes, no deletions, strict (up-to-date) status
  checks.
- **Conventional Commits + SemVer enforcement.** Adopted
  [Conventional Commits 1.0](https://www.conventionalcommits.org/) for every
  change to the repo. Validation runs in two places: a `commit-msg`
  pre-commit hook (via [`commitizen`](https://commitizen-tools.github.io/commitizen/)),
  and a new `.github/workflows/commitlint.yml` workflow that re-validates every
  commit on every PR. SemVer bumps are driven by the same commit history —
  `uv run cz bump` walks commits since the last tag, computes the right
  `patch`/`minor`/`major` jump, rewrites `pyproject.toml` (single source of
  truth via `version_provider = "pep621"`), and tags `v$version`. While the
  project is pre-1.0, breaking changes bump the **minor** component per
  SemVer's `major_version_zero` carve-out. The hand-curated CHANGELOG stays
  authoritative (`update_changelog_on_bump = false`) so release notes
  continue to be written for humans. See `CONTRIBUTING.md` for the type
  cheat sheet and examples.
- **GitHub outcome-metric parity.** Thread *resolution* is now read via
  GitHub's GraphQL `reviewThreads` API (`github.graphql`,
  `get_pr_review_threads`, `classify_graphql_thread_outcome`), so
  `--sync-outcomes` reports real resolved/unresolved counts for GitHub
  instead of always-`false`. Comment correlation matches on both the REST
  integer `databaseId` and the GraphQL node `id`, so it works regardless of
  which id the post path stored. Falls back to the resolution-blind REST
  classifier if GraphQL is unavailable. New
  `--backfill-github-bot-comments-since ISO_TS` mirrors the GitLab backfill,
  importing already-posted bot review threads (with real resolution state)
  into local SQLite.
- **Confidence threshold for posting.** `[review].min_confidence` (default
  `0.85`) drops findings below the threshold before any GitLab call. Each
  drop emits a `finding_filtered` log event with the reason.
- **Allowed-kinds whitelist.** `[review].allowed_kinds` (default `[]` =
  no filter) restricts posting to findings whose `severity`, `category`,
  or `type` appears in the list, case-insensitive. Combine with
  `min_confidence` to express policies like "post only blocking +
  security findings at 0.9 confidence or above".
- **GitHub pull-request support.** Set `[scm].provider = "github"` (or run
  `gh-review-poller`). New GitHub REST client (`github.py`, Link-header
  pagination + Bearer auth + retry), GitHub provider (`scm/github.py`:
  `gh repo clone` + `refs/pull/*/head` checkout, `line`+`side` comment
  anchors, posting via a GitHub MCP server with REST fallback, REST-based
  outcome classification). `bin/mcp-github` wrapper added.
- **Provider-agnostic poller.** New `scm` package with an `ScmProvider`
  protocol and `get_provider(cfg)` factory; `poll`/`worker`/`sync_outcomes`
  drive the provider without branching on the backend. GitLab logic moved
  behind `scm/gitlab.py`.
- **TOML env-var interpolation.** Any string value in `config/env.toml`
  accepts `${VAR}` (required; fails with `ConfigError` if unset) and
  `${VAR:-default}` (optional fallback). Use `$$` for a literal `$`.
  Pairs with systemd `LoadCredential=` so tokens stay out of on-disk
  config.
- **`mr-review-poller --health` flag.** Reads the most recent
  `reviewed_mrs` row; exits `0` healthy, `1` stale (older than 3×
  `timeout_seconds`), `2` config error.
- **systemd + cron deploy templates.** `deploy/templates/llm-reviewer.{cron,service,timer}`
  with hardening defaults (`NoNewPrivileges=true`, `ProtectSystem=strict`,
  credential-based secret loading).
- **Cooperative SIGTERM/SIGINT shutdown.** The poll loop checks the
  shutdown flag between MRs and exits cleanly so in-flight workers finish
  under their own timeout instead of getting orphaned.
- **In-flight worker backpressure.** Poll cycles back off when
  `running` + `queued` MRs already exceed `max_merge_requests_per_poll` × 2.
- **Log correlation IDs.** Poll-cycle events carry `poll_run_id`; worker
  events carry `run_id`.
- **OTel readiness warning.** `configure_otel` no longer swallows init
  failures silently — emits one `otel_init_failed` JSON line on stderr
  and leaves itself retryable.
- **`[poller].state_dir` is now honored.** `paths.py` reads
  `LLM_CODE_REVIEW_BASE_DIR`, so runtime state can live on a different
  volume from the install root.
- **Detailed `config/env.example.toml`** — every section and key has an
  inline comment (purpose, safe default, footguns).
- **Release workflow with SBOM** — `.github/workflows/release.yml` builds
  the sdist + wheel on a `v*.*.*` tag, generates an SPDX SBOM via
  `anchore/sbom-action`, and publishes all three as release assets.
- **Live GitLab + GitHub integration tests** —
  `tests/integration/test_gitlab_live.py` and
  `tests/integration/test_github_live.py`, read-only, deselected from the
  default run (`-m 'not integration'`) and self-skipping without
  `LLM_REVIEWER_IT_*` credentials. `.github/workflows/integration.yml` runs
  both nightly/on-demand when the matching `IT_GITLAB_TOKEN` /
  `IT_GITHUB_TOKEN` secret is set. Regression guard for the API-shape class
  of bug (GitLab `classify_discussion_outcome`, GitHub PR/review-comment
  payload shape, Link-header pagination).

### Changed
- **Renamed upstream MCP wrappers** to disambiguate "MCP servers we
  consume" from "the MCP server we expose": `bin/mcp-gitlab` →
  `bin/mcp-upstream-gitlab`, `bin/mcp-github` → `bin/mcp-upstream-github`.
  Internal references (`mcp.py`, `scm/github.py`,
  `deploy/templates/codex-config.toml`, `tests/test_deploy_layout.py`)
  updated in lockstep. **Operator action required:** update your
  `~/.codex/config.toml` (or any other MCP client config) to point at the
  new wrapper paths.
- **Decomposed the `poller.py` god-module** into single-concern modules:
  `db.py` (SQLite schema + writers), `mcp.py` (GitLab MCP JSON-RPC),
  `subproc.py` (bounded subprocess + process-group kill, shared with
  `codex_runner`), `secrets.py` (credential redaction), `signals.py`
  (shutdown), `events.py` (JSON-line logging), `types.py` (shared
  `JsonObject`). `poller.py` is now pure orchestration; test-facing
  symbols are re-exported via `__all__`.
- **Consolidated duplicated definitions** — `JsonObject` (was in 4 files),
  the GitLab-token env-name list, and subprocess-runner boilerplate each
  now live in exactly one place.
- **`codex_runner` uses the shared `run_bounded`** — manual reviews get
  the same wall-clock timeout + process-group kill as poller-driven ones.
- **Provider-aware LLM API key export.** `[agents].llm_api_key` is mapped
  to the provider env var matching `[agents].llm_model` (gpt → OpenAI,
  claude → Anthropic) plus the generic `LLM_API_KEY`, instead of being
  fanned out into every provider name.
- **Config field names match TOML keys exactly** — `ReviewConfig` uses
  `max_merge_requests_per_poll`, `max_findings_per_merge_request`,
  `timeout_seconds`, `target_merge_request_iid`. One vocabulary for
  operators and programmers.
- **Manual-wrapper dry-run is `[agents].dry_run`** (was
  `manual_review_dry_run`).
- **README** restructured to a linear onboarding flow and prerequisites
  filled in (`glab`, GitLab MCP server, Superpowers + `code-reviewer`
  skill, bot user, LLM key).
- **Docstrings** added to every module under `src/llm_reviewer/`.

### Removed
- **`bin/code-review-claude`** — orphaned wrapper, zero references.
- **`manual_review_dry_run` legacy alias** and the speculative
  `o1`/`o3`/`o4`/`qwen` provider mappings — pre-emptive flexibility for a
  project with no prior release.
- **Dead `LLM_CODE_REVIEW_HOME` export** — no consumer.

### Fixed
- **`gh-review-poller` provider override now takes effect.**
  `load_review_config` reads the `LLM_REVIEWER_PROVIDER` env var and
  overrides `[scm].provider` from the on-disk config, so the GitHub entry
  point actually forces `provider = "github"` instead of silently falling
  back to whatever `env.toml` declares. Regression test added.
- **Uniform position-field logging.** `finding_planned` no longer uses a
  key-presence check while `finding_posted`/`finding_pending_external_id`
  use a truthy fallback — all three now go through `_position_file` /
  `_position_line`, so a falsy `new_path` resolves to `path` consistently
  across providers.
- **`count_inflight_workers`** no longer carries an unused parameter whose
  docstring described filtering the code never did.
- **Stale config error labels** now name the real TOML key
  (`max_findings_per_merge_request`).
