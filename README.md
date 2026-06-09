# Bubo 🦉

> **Agentic AI code review — with the LLM of your choice.**

[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-3776ab?logo=python&logoColor=white)](pyproject.toml)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-2f3542)](pyproject.toml)
[![CI](https://github.com/mountainowl/bubo/actions/workflows/ci.yml/badge.svg)](https://github.com/mountainowl/bubo/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/mountainowl/bubo/badge)](https://scorecard.dev/viewer/?uri=github.com/mountainowl/bubo)
[![Docs](https://img.shields.io/badge/docs-mountainowl.github.io%2Fbubo-4f62ad)](https://mountainowl.github.io/bubo/)
[![License: MIT](https://img.shields.io/badge/license-MIT-111827)](LICENSE)

Bubo is an **agentic AI code reviewer** for GitLab MRs and GitHub PRs. It watches
open changes, runs a structured agentic review with **the LLM you choose** (Codex,
Claude, or any model your CLI drives), and posts only actionable findings as inline
review threads — no chatbot noise, no praise, no summaries. Like the owl it's named
for, it stays silent until it has something worth saying.

![Bubo hero](docs/images/bubo-hero.png)

## 📖 Documentation

**Full, rendered docs live at → [mountainowl.github.io/bubo](https://mountainowl.github.io/bubo/)**
(the canonical reference). The `docs/*.md` files below are the source for that
site; this README is a teaser.

👉 New here? The **[Recipes](https://mountainowl.github.io/bubo/recipes/)**
([docs/recipes.md](docs/recipes.md)) are copy-paste setups for GitLab and
GitHub — using Codex (the bundled default) or Claude as the review agent.

## What a review looks like

Findings are posted inline in a fixed shape — `Issue` / `Impact` / `Evidence` /
`Fix` / `Confidence`:

```text
Issue: HS256 JWT fallback is skipped when Cognito URL construction fails.
Impact: Valid local/shared-secret JWT requests return 500 instead of authenticating.
Evidence: The changed interceptor rethrows InvalidAwsUrlException before fallback runs.
Fix: Treat Cognito validation construction failures as failed Cognito auth when fallback is allowed.
Confidence: 0.94
```

When a review finds nothing actionable, Bubo posts one short change-level
acknowledgement (`Automated review ran — no issues found.`) so a clean MR/PR is
distinguishable from one the reviewer never touched. It's default-on, dedup'd by
bot author + exact body, and configurable under `[agents]` (see the
[configuration reference](docs/configuration.md)).

Real (sanitized) inline findings on GitLab MRs:

![Sanitized inline finding — data primer](docs/images/gitlab-mr-review-data-primer.png)

![Sanitized inline finding — exception handler](docs/images/gitlab-mr-review-exception-handler.png)

More sanitized examples are in [docs/examples/README.md](docs/examples/README.md).
Demo GIF: [docs/media/bubo-demo.gif](docs/media/bubo-demo.gif).

## 60-second quickstart

Install prereqs (uv, Python 3.14+, Git, plus the CLI for your SCM and a Codex
agent — see [prerequisites](docs/prerequisites.md)), then:

```sh
uv tool install git+https://github.com/mountainowl/bubo@v0.8.0
bubo init                # idempotent; --dry-run to preview

# Edit ~/.local/share/bubo/config/env.toml:
#   [gitlab].token, [agents].llm_model, [agents].llm_api_key,
#   [agents].llm_api_key_env, and at least one [[projects]] entry.

bubo doctor              # verify before first poll
bubo-poller              # one poll cycle; exits at the end
```

The first cycle runs with `[review].dry_run = true` (the default) — findings are
planned but no comments are posted. Flip to `false` once a real review looks
right. The full walkthrough is in the
**[Recipes](https://mountainowl.github.io/bubo/recipes/)** and
[install and configure](docs/install-and-configure.md); poller flags and the
bundled MCP server are in [run](docs/run.md).

## Further reading

These render on the [docs site](https://mountainowl.github.io/bubo/) and as
plain Markdown in the repo:

| Doc | What's in it |
|---|---|
| [Prerequisites](docs/prerequisites.md) | macOS / Linux runtime, per-provider tools, credentials, install verification. |
| [Install and configure](docs/install-and-configure.md) | `uv tool install`, `bubo init`, the minimum `config/env.toml`, GitLab and GitHub bot setup. |
| [Run](docs/run.md) | One-off review, the poller, the bundled `bubo-mcp` MCP server, and upstream wrappers. |
| [Configuration reference](docs/configuration.md) | Every `[scm]` / `[gitlab]` / `[github]` / `[review]` / `[poller]` / `[agents]` / `[telemetry]` / `[[projects]]` setting and its default. |
| [Operate](docs/operate.md) | Remote deploy, scheduling under cron or systemd, `--sync-outcomes` grading, one-shot backfill. |
| [Telemetry](docs/telemetry.md) | Emitted `llm_review.*` metrics, ready-made dashboard queries, cardinality discipline. |

## Status

- **GitLab & GitHub posting via polling** — production path, at outcome-metric
  parity. Set `[scm].provider = "github"` (or run `bubo-gh-poller`).
- **MCP server (`bubo-mcp`)** — read-only metrics + triggered reviews; stdio or HTTP.
- **Bring your own agent** — the review runs through the CLI you set in
  `[agents].reviewer_command`; **Codex** ships as the bundled default and
  **Claude** (`claude -p`) is a drop-in alternative.
- **Webhook-driven triggering** — not implemented; polling is the only path.

Review execution is intentionally outside CI/CD. Run it as a poller beside your existing pipelines.

## Security

- `config/env.toml` is gitignored and holds tokens. **Do not print or commit
  real values from it.**
- Review-agent stdout is redacted (`GITLAB_TOKEN=`, `OPENAI_API_KEY=`, `glpat-…`,
  `sk-…`, and credentialed Git URLs) before being written to reports, logs, or
  the database error column.
- The reviewer subprocess is launched under a strict env allowlist — host
  secrets are not passed wholesale into the LLM agent. Releases are cosign-signed
  with an SBOM. Report vulnerabilities per [`SECURITY.md`](SECURITY.md).

## Bot avatar

Upload [`assets/bubo.png`](assets/bubo.png) as the GitLab (or future GitHub) bot avatar.

![Bubo avatar preview](docs/images/bubo-avatar-preview.png)

## Community

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) ·
[Support](SUPPORT.md) · [Code of conduct](CODE_OF_CONDUCT.md)
