# Changelog

All notable changes to this project are tracked in this file.

This project follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
formatting and [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries land here on the same PR that ships the change. The first
production tag (`0.1.0`) cuts everything currently under "Unreleased".

## [Unreleased]

### Added
- **Opt-in review-comment tone ("moods").** `[review].tone` chooses the *voice*
  of a posted finding: `terse` (default — unchanged structured render),
  `collaborative`, `socratic`, `formal`, or `casual`. For any non-default tone
  bubo injects a short voice directive (register + one cross-domain style
  example) into the review prompt, asking the reviewer for an in-voice
  `comment` field that is posted in place of the structured render. The
  structured fields **and the dedup fingerprint stay mood-neutral**, so
  switching tone never re-posts a finding, splits its accept/dispute history,
  or perturbs the governance dataset — only the words developers read change.
  `terse` is byte-identical to prior behavior (no prompt change, no extra
  tokens). See [docs/configuration.md](docs/configuration.md), "Review-comment
  tone".
- **Feedback-loop & measurement surfaces in the governance report (read-only).**
  Three signals bubo already captures are now queryable via `bubo report` and
  MCP, so operators and governance teams can *see* the loop rather than have it
  act silently. (1) A new `dispute_classes` report section lists per-category
  dispute rates (`{category, total, rejected, dispute_rate}`, ordered by rate)
  from `db.disputed_class_stats` — the **raw, config-independent** truth behind
  dispute-driven suppression. A dedicated `get_dispute_classes(project)` MCP
  tool additionally reads the operator's real
  `[review].dispute_suppress_threshold` / `dispute_suppress_min_samples` and adds
  a **truthful** `would_suppress` flag per category (never a hardcoded-threshold
  guess; falls back to raw stats if config is unreadable). (2) A `latency`
  section reports review-run wall-clock latency (`count`, p50/p95/max/avg
  seconds) from `db.latency_summary`. (3) An `acknowledgements` rollup nested in
  `reviews` makes the `{no_findings, success, failed}` status counts first-class,
  mirroring `reviews.by_status`. `dispute_classes` is CSV-exportable
  (`--section dispute_classes`); `latency`/`acknowledgements` are JSON-only. All
  strictly read-only (never call `init_db`). See
  [docs/operate.md](docs/operate.md), "Governance report".
- **Read-only governance report / export (`bubo report` + `get_governance_report`).**
  A new read-only CLI command and matching MCP tool assemble an auditable
  governance report from the metrics already in SQLite: review counts, a
  **provenance breakdown** (counts by band/source), the **accept-vs-dispute
  rate**, a **noise trend** (daily false-positive trend), a **bug-catch ROI
  proxy**, token/cost rollups, **policy-decision stats** (from the Phase 2
  `governance_decisions`), and a per-change **audit trail**. `bubo report`
  emits the full nested report as JSON (`--format json`, the default) or a
  single tabular section as CSV (`--format csv --section {audit,noise_trend}`,
  default `audit`); scalar rollups are JSON-only. Windowing via `--since-hours`
  (default 24) or fixed `--since`/`--until` ISO dates, plus `--project`,
  `--limit`, and `--root`. `get_governance_report(since_hours, since, until,
  project)` returns the same nested JSON to chat clients. **Strictly read-only**
  (never mutates review state — safe from a monitoring cron) and **self-hosted**
  (data never leaves your infrastructure). Policy-decision stats populate only
  when Phase 2 governance is enabled; otherwise that section reports
  `available: false`. See [docs/operate.md](docs/operate.md), "Governance
  report".
- **Opt-in governance rigor modulation + policy gates (Phase 2).** Builds on
  provenance capture. Two advisory, off-by-default capabilities that *use* the
  captured signal: (1) `[governance].rigor_modulation` injects a
  heightened-scrutiny directive (prioritize the security lens) into the review
  prompt of a change that **escalates**; (2) `[governance].policy_mode`
  (`off`/`report-only`/`soft`) records an auditable, **write-once** governance
  *decision* per change (`flag`/`clear`) in a new `governance_decisions` table,
  surfaced as a `governance_decision` log event + `llm_review.governance`
  metric and queryable for audit. Escalation uses one shared predicate —
  band ∈ `escalate_bands` (default `likely_ai`+`collaborative`; `unknown` never
  escalates) and, unless `rigor_require_sensitive = false`, a
  `sensitive_path_globs` hit. Enabling either auto-implies the provenance fetch.
  **All advisory** — there is no `enforce` mode; bubo cannot block a merge. New
  pure `bubo.governance_policy` module. See
  [docs/configuration.md](docs/configuration.md), "Phase 2 — rigor modulation &
  policy gates".
- **Opt-in AI-code provenance capture (governance).** For regulated/enterprise
  teams, bubo can now record a per-change **provenance signal** for audit —
  self-hosted, so the data never leaves your infrastructure. Gated behind
  `[governance].capture_provenance` (default `false`); when off, no commit/diff
  metadata is fetched and nothing is recorded. bubo reads the change's commit
  trailers (it already checks the change out) and stores a *banded* signal on
  the review run: `band` ∈ `unknown` / `likely_ai` / `collaborative`, plus a
  `source` (`trailer` — deterministic; LLM `detection` deferred), the matched
  declaration lines, and any `sensitive_path_globs` hits. Two honesty rules are
  built in: it is a band **never a binary verdict**, and `unknown` is the
  default (absence of a declaration is not proof of human authorship —
  declared ≠ detected). Persisted **write-once** for audit integrity; surfaced
  as a `provenance_captured` log event and the `llm_review.provenance` metric.
  **Captures only — changes no review behavior** (rigor modulation and policy
  gates are a later, separately opt-in phase). bubo does not block merges; it
  produces auditable data your pipeline acts on. See
  [docs/configuration.md](docs/configuration.md), "Governance & provenance".
- **Opt-in dispute-driven noise suppression.** Bubo already records, per
  finding, whether the developer accepted or disputed it. The new
  `[review].suppress_disputed_classes` flag (default `false`) turns that
  history into a per-repo precision lever: when enabled, the poller stops
  posting finding `category` classes a team has repeatedly rejected, instead
  of re-litigating the same noise every MR. A category is suppressed only
  when at least `[review].dispute_suppress_min_samples` (default `5`) of its
  findings have a recorded outcome **and** the dispute rate is at or above
  `[review].dispute_suppress_threshold` (default `0.5`); the denominator
  counts all outcomes (including sync-failure rows), biasing toward
  under-suppression so a useful class is never silenced off a thin signal.
  Suppressed findings are logged with reason `disputed_class_suppressed`.
  Off by default and self-reinforcing once on — see
  [docs/configuration.md](docs/configuration.md), "Dispute-driven
  suppression", for the caveat and escape hatches.
- **Outcome sync now classifies developer replies as accept/reject.** A
  thread resolved *after a rebuttal* ("working as intended", "not a
  blocker") used to look identical to one resolved *because the fix
  landed*, so `resolved` overcounted the reviewer's precision. When a
  finding's discussion has a developer reply and no explicit dispute
  marker, `--sync-outcomes` now asks an LLM to read the finding plus the
  reply and decide; a rejection sets `disputed` (and `false_positive` when
  the reply says the finding is factually wrong). Model-agnostic — it
  reuses `[agents].reviewer_command` (the default is
  `codex exec --profile bubo`; any prompt-taking CLI such as `claude -p`
  works too). Each finding is classified
  once (new `finding_outcomes.reply_classified` column) to bound cost, and
  classifications are capped per sync run so the first post-upgrade sync
  cannot fire the whole backlog at once (it drains over later runs).
  Transient classifier failures are retried on a later sync and never
  block it.
  Explicit `[llm-review:disputed]` / `[llm-review:false-positive]` markers
  still short-circuit the LLM call. See
  [docs/operate.md](docs/operate.md), "Reply classification".

### Fixed
- **`[telemetry]` now rejects quoted booleans instead of silently misreading
  them.** A quoted `enabled = "false"` (or `emit_finding_events`/
  `emit_outcome_sync`) was parsed with bare `bool()`, which treats any
  non-empty string as truthy — so `"false"` *enabled* telemetry, the opposite
  of the operator's intent. These now parse through the strict
  `config_values` helpers and raise a `ConfigError` for quoted booleans /
  malformed strings (telemetry then disables via the existing
  load-error fallback rather than coming up misconfigured). Telemetry written
  with real TOML booleans (`enabled = true`) is unaffected.

### Changed
- **Internal: the GitLab and GitHub REST clients now share one HTTP
  transport (`bubo._http`).** The retry/backoff loop, retryable-status set,
  and `Retry-After` handling were duplicated across `bubo.gitlab` and
  `bubo.github` (byte-identical in places) and had begun to drift. They are
  consolidated into a single `request_json`; each client keeps only its own
  dialect (URL/auth/pagination) and passes provider-specific retry conditions
  via a hook (GitHub's 403 primary rate-limit). No change to request behavior;
  the shared loop gains direct test coverage it previously lacked.
- **BREAKING: the `bin/` launchers are consolidated into a single `bin/bubo`
  dispatcher.** `bin/bubo-poller`, `bin/bubo-mcp`, `bin/mcp-upstream-gitlab`,
  `bin/mcp-upstream-github`, and `bin/bubo-env` are replaced by subcommands —
  `bin/bubo poll`, `bin/bubo mcp`, and `bin/bubo mcp-upstream <github|gitlab>`
  (the env-loading the former `bin/bubo-env` did is now an internal function
  each subcommand calls). The cron/systemd templates and the Codex MCP config
  (`codex-config.toml`) now point at `bin/bubo …`. **Migration:** re-run
  `bubo init` so `~/.codex/config.toml` is re-stamped with the new command
  paths, and update any hand-written cron/systemd units or MCP client config
  that referenced the old `bin/` paths.
- **BREAKING: agent execution is unified — `[agents].reviewer_command` is
  run directly, with no bundled wrapper.** The `bubo-codex` console script,
  `bin/bubo-codex`, and `src/bubo/codex_runner.py` are removed. Codex is now
  just another configured command: the default `reviewer_command` is
  `codex --ask-for-approval never exec --profile bubo --skip-git-repo-check`
  (the same invocation the wrapper used), and any agent CLI that takes a
  prompt argument (e.g. `claude -p`) is configured the same way. The review
  contract + skill instruction travel in the review prompt (`REVIEW_CONTRACT`
  via `provider.review_prompt`), so Codex loads the `code-reviewer` skill
  without the wrapper — verified against live public-repo reviews. The MCP
  `review_change` tool now builds that same contract-carrying prompt instead
  of a bespoke task string. The poller's `reviewer_env` no longer exports the
  now-dead `BUBO_PROMPT` / `LLM_REVIEW_MAX_FINDINGS` / `BUBO_SKIP_AGENT_CONFIG_ENV`.
  **Migration:** anyone who invoked `bubo-codex` / `bin/bubo-codex` directly
  should run the configured agent instead (e.g.
  `codex exec --profile bubo "Review the current changes."`); default poller
  and reply-classifier behavior is unchanged.
- **`bin/env` renamed to `bin/bubo-env`.** The shared environment-loader
  launcher now follows the `bubo-*` naming convention and no longer shadows
  the system `env` command. All bundled launchers reference the new path;
  this is breaking only for automation that invoked `$BUBO_ROOT/bin/env`
  directly (the supported entry points are the `bubo-*` console scripts).
  Every `bin/` launcher also gained a header comment documenting what it does.
- **Docs: the GitHub Pages site is now the canonical reference, and the
  README/MD files are teasers that link to it.** Added a copy-paste
  [Recipes](docs/recipes.md) page — Codex (the bundled default) gets worked
  GitLab and GitHub recipes, with a short Claude section for reviewing
  through the Claude CLI instead. Surfaced it
  in the MkDocs nav and on the Overview page, and trimmed the README from a
  full manual to a teaser (kept the visuals, example output, badges,
  quickstart, and doc links; moved the deep "how it works" walkthrough to
  the docs site). Refreshed every stale `@v0.6.0` install pin to `@v0.8.0`
  and removed the already-passed "removed in v0.7.0" deprecation deadline.

### Removed
- **BREAKING: the `bubo-gh-poller` entry point is removed.** It was a thin
  alias for `bubo-poller` with the provider forced to GitHub. Drive GitHub
  reviews with `[scm].provider = "github"` (persistent) or
  `BUBO_PROVIDER=github bubo-poller` (single run) instead — both already
  supported and unchanged.
- **BREAKING: the deprecated shell installers `scripts/install-package.sh`
  and `scripts/deploy-package.sh` have been deleted.** They were deprecated
  in v0.6.0 (#22) in favor of `uv tool install` + `bubo init`. Operators
  still on the shell-installer path must switch to the supported flow:
  `uv tool install git+https://github.com/mountainowl/bubo@<tag>`, then
  `bubo init` and `bubo doctor` (see [docs/operate.md](docs/operate.md),
  "Migrating from the shell installer"). State (`var/state/reviewer.sqlite`)
  and `config/env.toml` are preserved across the migration.

## [0.8.0] - 2026-06-08

### Changed
- **BREAKING (config): the LLM API-key env var is now operator-named, not
  inferred from the model.** Bubo is "bring your own LLM," but the
  credential plumbing hardcoded a model→env map (`gpt → OPENAI_API_KEY`,
  `claude → ANTHROPIC_API_KEY`) and silently did nothing for any other
  provider (Gemini, Qwen, Mistral, local, …). That map is removed. Add
  `[agents].llm_api_key_env` to name the variable your LLM CLI/SDK reads
  the key from; the key in `[agents].llm_api_key` is exported under that
  name plus the generic `LLM_API_KEY`. The shipped `env.example.toml`
  defaults it to `OPENAI_API_KEY` (matching the default `gpt-5.5` +
  Codex), so fresh OpenAI installs are unaffected.

  **Migration:** operators who relied on the old `claude → ANTHROPIC_API_KEY`
  inference must add `llm_api_key_env = "ANTHROPIC_API_KEY"` to
  `[agents]`. OpenAI users on the example config need no change. Anyone
  on Gemini/other can now set the correct name instead of being stuck
  with a key that only exported under `LLM_API_KEY`.

## [0.7.2] - 2026-06-08

### Changed
- **Repository renamed** `mountainowl/ai-code-review` →
  [`mountainowl/bubo`](https://github.com/mountainowl/bubo) for full
  brand consistency. GitHub redirects the old paths, so existing clones,
  links, and `uv tool install git+https://github.com/mountainowl/ai-code-review@…`
  commands keep working — but update to the `…/bubo` URL when convenient.
  The docs site moved to `https://mountainowl.github.io/bubo/`.

### Fixed
- **Release artifacts now actually publish.** `v0.7.0` and `v0.7.1` both
  cut a GitHub Release that was missing the wheel, sdist, and deploy
  bundle. Root cause, finally nailed from the actual run log: cosign v3
  (installed by `cosign-installer`) writes a single Sigstore `.bundle`
  per artifact and **ignores** the legacy `--output-signature` /
  `--output-certificate` flags (`"deprecated when using
  --new-bundle-format and will be ignored"`). The publish step still
  globbed for `dist/*.sig`, matched nothing, and `fail_on_unmatched_files:
  true` aborted before the artifacts uploaded. Fixed by signing with
  `--bundle` only and publishing `dist/*.bundle`. Verify an artifact
  with `cosign verify-blob --bundle <artifact>.bundle …`.

## [0.7.1] - 2026-06-08

### Fixed
- **GitHub Pages docs site deploys.** The `deploy-docs.yml` workflow
  pinned `actions/deploy-pages` to a nonexistent commit SHA (and
  `actions/upload-pages-artifact` to a mislabeled one), so the site
  404'd. Repinned both to valid `v5.0.0` SHAs verified against the
  GitHub API.
- **Release signing (first attempt).** Added a cosign `--bundle` output
  to `sign-blob` to clear the `v0.7.0` `create bundle file: open :`
  error. This unblocked *signing* but the release still published
  without artifacts — the publish glob expected `.sig`/`.pem` files
  that cosign v3 no longer writes. Fully resolved in **[0.7.2]**.

### Added
- **References & further reading page** on the docs site — curated,
  link-verified citations for the sources behind Bubo's design,
  packaging, and discoverability work.

## [0.7.0] - 2026-06-08

### Changed
- **BREAKING: renamed the project to `bubo`.** The package, CLI commands,
  Codex profile, environment-variable namespace, install path, and all
  branding moved from `llm-reviewer` to **Bubo** 🦉 — an agentic AI code
  reviewer that runs the LLM of your choice. The GitHub repository slug
  (`mountainowl/ai-code-review`) is unchanged. Rename map:

  | Old | New |
  |---|---|
  | package `llm_reviewer` | `bubo` |
  | dist / `uv tool install` name `llm-reviewer` | `bubo` |
  | CLI `llm-reviewer` | `bubo` |
  | CLI `mr-review-poller` | `bubo-poller` |
  | CLI `gh-review-poller` | `bubo-gh-poller` |
  | CLI `code-review-codex` | `bubo-codex` |
  | CLI `mcp-llm-reviewer` | `bubo-mcp` |
  | Codex profile `[profiles.llm-reviewer]` | `[profiles.bubo]` |
  | env `LLM_CODE_REVIEW_*`, `LLM_REVIEWER_*` | `BUBO_*` |
  | install root `~/.local/share/llm-reviewer` | `~/.local/share/bubo` |
  | deploy templates `llm-reviewer.{cron,service,timer}` | `bubo.{cron,service,timer}` |

  External contracts are untouched: `GITLAB_TOKEN`, `GITHUB_TOKEN`,
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LLM_API_KEY`, the `REVIEW_*` /
  `CODEX_*` subprocess vars, and the `llm_review.*` OpenTelemetry metric
  namespace (kept stable so existing dashboards keep working).

  **Migration for existing operators** (one-time):

  ```sh
  # 1. Replace the install.
  uv tool uninstall llm-reviewer
  uv tool install git+https://github.com/mountainowl/ai-code-review@v0.7.0

  # 2. Move your state + config to the new root.
  mv ~/.local/share/llm-reviewer ~/.local/share/bubo    # SQLite state + env.toml

  # 3. In config/env.toml, rename any LLM_CODE_REVIEW_* / LLM_REVIEWER_*
  #    overrides to BUBO_* (most operators have none).

  # 4. Re-stamp agent config + cron/systemd templates under the new name.
  bubo init
  bubo doctor

  # 5. If you scheduled via cron/systemd, reinstall the renamed templates
  #    ($BUBO_ROOT/deploy/templates/bubo.{cron,service,timer}) and remove
  #    the old llm-reviewer.* units.
  ```

  The old `[profiles.llm-reviewer]` block in `~/.codex/config.toml` can be
  deleted after `bubo init` writes `[profiles.bubo]`.

### Added
- **New `bubo` CLI with `init` and `doctor` subcommands.** Closes #22.
  Operators now install via `uv tool install git+https://github.com/mountainowl/ai-code-review@<tag>`
  and run `bubo init` to place per-host assets (Codex profile,
  Claude settings, `config/env.toml` seed, `var/` workspace, prompts,
  skills, plugins, SQLite schema). `init` is idempotent, supports
  `--dry-run` / `--force` / `--no-agent-config` / `--root`, and writes
  `~/.codex/config.toml` with a load-bearing `[profiles.bubo]`
  block — the v0.5.0 incident's root cause (#20 bug 1) is locked out by
  a doctor check that asserts the block is present. `bubo
  doctor` is a non-mutating diagnostic: zero exit on a clean install,
  non-zero if env.toml is missing, the DB hasn't been initialized, or
  the Codex profile block isn't there. Suitable as a cron / monitoring
  smoke test.

### Changed
- **Build backend swapped from `uv_build` to `hatchling`.** uv_build's
  data-include shape was too narrow to ship the deploy assets (prompts,
  skills, plugins, deploy templates, env.example.toml) at
  `importlib.resources`-reachable locations; the silent-drop of those
  assets was #20 bug 3. Hatchling's `force-include` maps each repo-root
  asset into the wheel under `bubo/_assets/` so the new CLI
  can read them via `importlib.resources` regardless of install path
  (`uv tool install`, `pipx`, `pip`, or editable `uv sync --dev`).
  Recommended by uv maintainer konstin in astral-sh/uv#11502 for
  projects with non-Python assets. uv still owns dependency resolution
  and the lockfile.

### Deprecated
- **`scripts/install-package.sh` and `scripts/deploy-package.sh`.** Still
  functional in v0.6.x but print a deprecation warning on every run
  pointing at `uv tool install` + `bubo init`. Scheduled for
  removal in v0.7.0. The deploy assets and shell scripts continue to
  ship in the sdist so existing operators are unaffected during the
  deprecation window.

## [0.5.1] - 2026-06-04

### Fixed
- **Codex profile written in a shape Codex doesn't load (production-
  blocking).** The runtime invokes `codex --profile bubo`,
  which Codex resolves against `[profiles.bubo]` in
  `~/.codex/config.toml`. The installer was writing the profile to a
  sibling `~/.codex/bubo.config.toml` file that Codex does NOT
  auto-load for `--profile`, so every review aborted with `config
  profile bubo not found`. Fixed by inlining the profile as a
  `[profiles.bubo]` block inside `deploy/templates/codex-config.toml`,
  dropping the orphaned `codex-profile.toml`, and adding a post-install
  smoke check (`codex exec --profile bubo --skip-git-repo-check
  "Return exactly: profile-ok"`) that warns loudly when the profile
  does not round-trip. Closes #20 (Bug 1).
- **Stale `uv.lock` shipped at release tag → `uv sync --locked` fails
  on every install.** release-please-action bumps `pyproject.toml` but
  does not regenerate `uv.lock`; the resulting release PR's CI failed,
  and admin-merging it shipped a broken tarball. Fixed in two layers:
  the v0.5.0 lockfile drift is committed (HEAD now matches
  `pyproject.toml`), and a new `release-please-lockfile.yml` workflow
  fires on release-please PRs to regenerate `uv.lock`, commit it back
  under the release-please bot identity, and push so a fresh CI run
  goes green before the operator merges. Closes #20 (Bug 2).
- **sdist did not contain `config/`, `scripts/`, `deploy/`, `bin/`,
  `prompts/`, `skills/`, or `plugins/`.** The `uv_build` backend
  packages only the Python package by default; operators downloading
  the sdist couldn't run `install-package.sh`. The release workflow now
  builds a separate cosign-signed `bubo-deploy-X.Y.Z.tar.gz`
  bundle (full deployable tree, same exclude pattern as
  `scripts/deploy-package.sh`) and attaches it to every release.
  Install docs updated to point at the bundle and to call out that
  sdist/wheel are not the deploy contract. Closes #20 (Bug 3).
- **Cron template shipped no locking guidance.** The example cron in
  `deploy/templates/bubo.cron` was three bare invocations
  with no `flock` — operators repeatedly invented their own locking,
  and a single shared `poller.lock` (vs. separate locks per role)
  caused the `*/5` health probe to block the `*/15` poll at the `:45`
  collision. The template now ships with `flock -n` on a dedicated
  lockfile per role (`poller.lock`, `outcome-sync.lock`, `health.lock`)
  and an explanatory comment about why they must stay separate. Closes
  #20 (Bug 4).

## [0.4.1] - 2026-06-04

### Changed
- **README split into focused docs/ files.** README went from 863 → 172
  lines: hero, badges, example output (including the no-findings comment
  shipped in 0.4.0), a 60-second quickstart, the "How it works" diagram
  and step list, and a "Further reading" link table now sit on the top
  page. Deep content moved out to `docs/prerequisites.md`,
  `docs/install-and-configure.md`, `docs/run.md`,
  `docs/configuration.md`, `docs/operate.md`, and `docs/telemetry.md`.
  No operator-facing terminology or runtime behavior changes.

### Added
- **GitHub provider mechanics section** in `docs/run.md`: documents the
  MCP-prefers-then-REST-fallback posting path and the GraphQL-then-REST
  fallback used by `--sync-outcomes` for thread-resolution state. Linked
  from the README status table so operators evaluating the GitHub
  provider can find these details from the top page.

## [0.4.0] - 2026-06-03

### Added
- **"No issues found" comment on reviews with zero findings.** When a
  review completes with zero actionable findings, the poller now posts a
  single change-level comment (`"Automated review ran — no issues found."`
  by default) so authors and approvers can distinguish "reviewer ran and
  was happy" from "reviewer never ran." Behavior is on by default; gated
  by two new `[agents]` keys: `post_no_findings_comment` (bool, default
  `true` — set `false` to restore the previous silent behavior) and
  `no_findings_comment_body` (string — customize for localization or
  branding; whitespace-only disables the post even when the flag is on).
  Honors `[review].dry_run`. Idempotent on exact body match **scoped to
  the bot's author** — a human or other bot reproducing the body does
  not satisfy the dedup, so the reviewer never silently stops posting
  its own acknowledgement. Comment-post failures are **soft**: a
  transient API error from the acknowledgement post does NOT flip the
  underlying clean review to `FAILED`; the verdict is logged as
  `errored` and the review still records `NO_FINDINGS`. Implemented as
  a new `post_change_comment` method on the SCM provider protocol
  (GitLab MR notes, GitHub issue comments). Closes #13.
- **Strict TOML type validation for new config keys.** Added
  `bool_value` and `text_value` to `config_values.py` — they raise
  `ConfigError` for string `"false"` (which `bool()` would have silently
  coerced to `True`) and for lists/tables (which `str()` would have
  silently turned into a misleading repr). Applied to the two new
  `[agents]` keys; existing keys are unaffected.

## [0.3.0] - 2026-06-01

### Added
- **`bubo-mcp` server.** New MCP server with two interfaces
  exposed to any MCP-capable client (Codex, Claude Desktop, Cline).
  *Metrics interface* (read-only against SQLite): `health`,
  `list_recent_reviews`, `get_review`, `get_findings`,
  `get_finding_outcomes`, `get_metrics` (aggregate counts + token/cost
  sums over a configurable window). *Review interface*: `review_change`
  triggers a one-shot review by URL or
  `(provider={gitlab,github,auto}, project, number)`, blocks until the
  underlying `bubo-codex` subprocess finishes, and returns the
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
  `bin/bubo-mcp` launcher; new readers in `db.py`
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
  `bubo-gh-poller`). New GitHub REST client (`github.py`, Link-header
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
- **`bubo-poller --health` flag.** Reads the most recent
  `reviewed_mrs` row; exits `0` healthy, `1` stale (older than 3×
  `timeout_seconds`), `2` config error.
- **systemd + cron deploy templates.** `deploy/templates/bubo.{cron,service,timer}`
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
  `BUBO_BASE_DIR`, so runtime state can live on a different
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
  `BUBO_IT_*` credentials. `.github/workflows/integration.yml` runs
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
- **Docstrings** added to every module under `src/bubo/`.

### Removed
- **`bin/code-review-claude`** — orphaned wrapper, zero references.
- **`manual_review_dry_run` legacy alias** and the speculative
  `o1`/`o3`/`o4`/`qwen` provider mappings — pre-emptive flexibility for a
  project with no prior release.
- **Dead `BUBO_HOME` export** — no consumer.

### Fixed
- **`bubo-gh-poller` provider override now takes effect.**
  `load_review_config` reads the `BUBO_PROVIDER` env var and
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
