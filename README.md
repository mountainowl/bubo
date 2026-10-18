# Bubo 🦉

[![PyPI](https://img.shields.io/pypi/v/bubo?logo=pypi&logoColor=white)](https://pypi.org/project/bubo/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-3776ab?logo=python&logoColor=white)](https://github.com/mountainowl/bubo/blob/main/pyproject.toml)
[![Docker: GHCR](https://img.shields.io/badge/docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/mountainowl/bubo/pkgs/container/bubo)
[![GitHub Action](https://img.shields.io/badge/GitHub-Action-2088FF?logo=githubactions&logoColor=white)](https://mountainowl.github.io/bubo/github-action/)
[![CI](https://github.com/mountainowl/bubo/actions/workflows/ci.yml/badge.svg)](https://github.com/mountainowl/bubo/actions/workflows/ci.yml)
[![SCM adapters](https://img.shields.io/badge/SCM-adapters-2f3542)](https://mountainowl.github.io/bubo/features/)
[![OpenTelemetry](https://img.shields.io/badge/metrics-OpenTelemetry-4f62ad)](https://mountainowl.github.io/bubo/telemetry/)
[![OpenSSF Scorecard](https://img.shields.io/ossf-scorecard/github.com/mountainowl/bubo?label=OpenSSF%20Scorecard)](https://scorecard.dev/viewer/?uri=github.com/mountainowl/bubo)
[![Signed with cosign](https://img.shields.io/badge/release-cosign%20signed-2bb4ab?logo=sigstore&logoColor=white)](https://github.com/mountainowl/bubo/releases)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://slsa.dev)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-2f3542)](https://github.com/astral-sh/uv)
[![Docs](https://img.shields.io/badge/docs-mountainowl.github.io%2Fbubo-4f62ad)](https://mountainowl.github.io/bubo/)
[![License: MIT](https://img.shields.io/badge/license-MIT-111827)](https://github.com/mountainowl/bubo/blob/main/LICENSE)

<img src="assets/bubo-readme-features.gif" width="960" alt="Bubo AI code review tone examples">

This wise owl is **Bubo**, an **agentic AI code reviewer** that reviews your GitLab
MRs and GitHub PRs with the LLM model _of your choice_, and posts only the findings worth
acting on as inline threads — no chatbot noise, no praise, no summaries.

- **Signal over noise** — evidence-backed inline findings, confidence gates, optional verification, and deduplication.
- **Your model, your infrastructure** — OpenAI, Anthropic, or another CLI-backed reviewer with your own deployment.
- **Feedback-driven reviews** — recorded outcomes help tune thresholds and suppress recurring unwanted findings.
- **Review evidence, in one place** — local dashboards and reports for findings, usage, policy decisions, and outcomes.
- **Compliance** - cosign-signed releases with SBOMs, auditable on-prem report
- **Metrics and RoI** — [Read about real world metrics and ROI](https://mountainowl.github.io/bubo/operate/#governance-report)

> Run continuous reviews through [polling](https://mountainowl.github.io/bubo/recipes/),
> or trigger reviews through [MCP](https://mountainowl.github.io/bubo/mcp/). The experimental
> [GitHub Action](https://mountainowl.github.io/bubo/github-action/).

<img src="assets/bubo-dashboard-teaser.png" width="960" alt="Bubo dashboard, review activity, and reports shown as layered interface previews">

## Install

```sh
uv tool install bubo     # or: pipx install bubo
bubo init                # idempotent; seeds config + workspace + DB
bubo doctor              # verify before the first poll
bubo-poller service start # start the review service (polling + outcome sync)
```

Container → `docker pull ghcr.io/mountainowl/bubo` - Bring your own LLM CLI, you can use multiple architectures.

## Documentation

Everything lives on the docs site — this README is just the front door.

|                                                                        |                                                        |
| ---------------------------------------------------------------------- | ------------------------------------------------------ |
| [Recipes](https://mountainowl.github.io/bubo/recipes/)                 | Copy-paste GitLab / GitHub / in-house-model setups.    |
| [Features](https://mountainowl.github.io/bubo/features/)               | The full capability list.                              |
| [Configuration](https://mountainowl.github.io/bubo/configuration/)     | Every setting, per section, plus a quick-start config. |
| [Operate](https://mountainowl.github.io/bubo/operate/)                 | Deploy, schedule, grade outcomes, governance report.   |
| [Troubleshooting](https://mountainowl.github.io/bubo/troubleshooting/) | Host / infra fixes (sandbox, AppArmor).                |
| [Metrics & telemetry](https://mountainowl.github.io/bubo/telemetry/)   | Emitted `llm_review.*` metrics and dashboards.         |
| [Updates & recovery](docs/pages/auto-update.mdx)                       | Automatic/manual updates, safe deferrals and issue-ready failure reports; platform acceptance status. |

## Community

[Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md) · [Release verification](https://mountainowl.github.io/bubo/operate/#verify-a-release) · [Support](SUPPORT.md) · [Code of conduct](CODE_OF_CONDUCT.md) · [License: MIT](LICENSE)
