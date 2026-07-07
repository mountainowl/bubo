# Bubo 🦉

[![PyPI](https://img.shields.io/pypi/v/bubo?logo=pypi&logoColor=white)](https://pypi.org/project/bubo/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-3776ab?logo=python&logoColor=white)](https://github.com/mountainowl/bubo/blob/main/pyproject.toml)
[![Docker: GHCR](https://img.shields.io/badge/docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/mountainowl/bubo/pkgs/container/bubo)
[![CI](https://github.com/mountainowl/bubo/actions/workflows/ci.yml/badge.svg)](https://github.com/mountainowl/bubo/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://img.shields.io/ossf-scorecard/github.com/mountainowl/bubo?label=OpenSSF%20Scorecard)](https://scorecard.dev/viewer/?uri=github.com/mountainowl/bubo)
[![Signed with cosign](https://img.shields.io/badge/release-cosign%20signed-2bb4ab?logo=sigstore&logoColor=white)](https://github.com/mountainowl/bubo/releases)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://slsa.dev)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-2f3542)](https://github.com/astral-sh/uv)
[![Docs](https://img.shields.io/badge/docs-mountainowl.github.io%2Fbubo-4f62ad)](https://mountainowl.github.io/bubo/)
[![License: MIT](https://img.shields.io/badge/license-MIT-111827)](https://github.com/mountainowl/bubo/blob/main/LICENSE)

**Agentic AI code review with the LLM of your choice.** Bubo reviews your GitLab
MRs and GitHub PRs with the model *you* run, and posts only the findings worth
acting on as inline threads — no chatbot noise, no praise, no summaries.

-  **Self-hosted** — code, diffs, and review data stay on your infrastructure
-  **Bring-your-own-LLM** — Codex, Claude, or any model your CLI drives
-  **SCM** — Currently supports Gitlab and Github 
-  **findings** — Inline or "all good" if clean
-  **Governance, provenance & an auditable on-prem report** - cosign-signed releases with SBOMs
-  **Metrics** — Opentelemetry 

**Full documentation → [mountainowl.github.io/bubo](https://mountainowl.github.io/bubo/)**

## Install

```sh
uv tool install bubo     # or: pipx install bubo
bubo init                # idempotent; seeds config + workspace + DB
bubo doctor              # verify before the first poll
bubo-poller              # one poll cycle — dry-run by default, posts nothing
```

Prefer a container? `docker pull ghcr.io/mountainowl/bubo` (multi-arch; the
review-agent CLI is BYO). Full walkthrough in
[Install and configure](https://mountainowl.github.io/bubo/install-and-configure/).

## Documentation

Everything lives on the docs site — this README is just the front door.

| | |
|---|---|
| [Recipes](https://mountainowl.github.io/bubo/recipes/) | Copy-paste GitLab / GitHub / in-house-model setups. |
| [Features](https://mountainowl.github.io/bubo/features/) | The full capability list. |
| [Configuration](https://mountainowl.github.io/bubo/configuration/) | Every setting, per section, plus a quick-start config. |
| [Operate](https://mountainowl.github.io/bubo/operate/) | Deploy, schedule, grade outcomes, governance report. |
| [Troubleshooting](https://mountainowl.github.io/bubo/troubleshooting/) | Host / infra fixes (sandbox, AppArmor). |
| [Metrics & telemetry](https://mountainowl.github.io/bubo/telemetry/) | Emitted `llm_review.*` metrics and dashboards. |

## Status

- **GitLab & GitHub posting via polling** — production path, at outcome-metric
  parity. Set `[scm].provider = "github"` (or `BUBO_PROVIDER=github`).
- **MCP server (`bubo-mcp`)** — read-only metrics + triggered reviews; stdio or HTTP.
- **Codex or Claude** — Bubo runs the review through a wrapper around your agent
  CLI; Codex ships pre-wired.
- **Webhook-driven triggering** — not yet; polling is the only path.

Review execution sits outside CI/CD by design — run it as a poller beside your
existing pipelines.

## Security

- `config/env.toml` is gitignored and holds tokens. **Do not print or commit real values.**
- Review-agent stdout is redacted (`GITLAB_TOKEN=`, `OPENAI_API_KEY=`, `glpat-…`,
  `sk-…`, credentialed Git URLs) before it touches reports, logs, or the database.
- The reviewer subprocess runs under a strict env allowlist — host secrets aren't
  handed wholesale to the LLM agent.
- Releases are cosign-signed via Sigstore keyless OIDC, with an SBOM on every release.
- Report vulnerabilities per [SECURITY.md](SECURITY.md).

## Community

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Support](SUPPORT.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [License: MIT](LICENSE)
