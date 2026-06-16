# GitHub Action (CI review)

Run Bubo on a pull request straight from CI: it reviews the PR that triggered the
workflow and posts inline findings via the GitHub REST API — no MCP server, no
poller, no self-hosted host required. It's a composite action, so it runs on
**GitHub-hosted *and* self-hosted runners**, and your LLM key never leaves the
runner.

!!! warning "Experimental (v1) — validate before you rely on it"
    The Bubo *engine* (config → single-PR review → REST posting) is the same code
    the poller uses. The new part is the **BYO agent chain in CI**: the review
    still runs through an agent CLI (Codex/Claude) **and** the Superpowers
    `code-reviewer` skill, which must be present and authenticated **on the
    runner**. That chain is environment-specific and not yet validated across
    hosted runners — **prove it on one real PR (start with `dry-run: true`)
    before trusting it or publishing the action to the Marketplace.** Self-hosted
    runners that pre-provision + authenticate the agent are the most reliable path.

## Quick start

```yaml
# .github/workflows/bubo.yml
name: Bubo review
on:
  pull_request:

permissions:
  contents: read         # read the diff
  pull-requests: write   # post inline findings

jobs:
  review:
    runs-on: ubuntu-latest          # or your self-hosted runner label
    steps:
      - uses: mountainowl/bubo@v0   # pin a released tag once you've validated it
        with:
          llm-api-key: ${{ secrets.OPENAI_API_KEY }}
          dry-run: "true"           # plan only; flip to "false" to post
          install-agent: "true"     # hosted runners: best-effort Codex + Superpowers
          tone: "collaborative"
```

Start with `dry-run: "true"` and read the run logs / transcript. When findings
look right, set `dry-run: "false"` to post them inline.

## Inputs

| Input | Default | What it does |
|---|---|---|
| `llm-api-key` | *(required)* | Your review LLM key (e.g. an OpenAI key for Codex). Pass a secret. |
| `llm-api-key-env` | `OPENAI_API_KEY` | Env var your agent CLI reads the key from. |
| `llm-model` | `gpt-5.5` | Model label for cost/telemetry (the real model comes from your agent profile). |
| `github-token` | `${{ github.token }}` | Reads the diff + posts comments. Needs `pull-requests: write`. |
| `reviewer-command` | *(bundled Codex)* | Space-separated argv to run a different agent CLI (e.g. `claude -p`). |
| `dry-run` | `false` | `true` plans findings but posts nothing. |
| `tone` | `terse` | Review voice: `terse` / `collaborative` / `socratic` / `formal` / `casual`. |
| `min-confidence` | *(Bubo default)* | Drop findings below this confidence (0.0–1.0). |
| `max-findings` | *(Bubo default)* | Cap findings posted per PR. |
| `bubo-version` | *(latest)* | Pin a Bubo PyPI version. |
| `install-agent` | `false` | Best-effort install of Codex + Superpowers on a hosted runner. Leave `false` on self-hosted runners that pre-provision the agent. |

## Self-hosted runners (recommended)

Bubo's pitch is "nothing leaves your infra" — a self-hosted runner keeps that true
in CI too. Pre-provision the agent once on the runner image and leave
`install-agent: false`:

- Install the agent CLI (Codex or Claude) and authenticate it (e.g.
  `codex login --with-api-key`), or supply auth the runner already holds.
- Install **Superpowers** + the `code-reviewer` skill in the agent's config.
- Put `uv`, `git`, and the agent CLI on `PATH`.

The action then just installs Bubo from PyPI, writes config from your inputs,
scopes the review to the triggering PR, and posts.

## What it does under the hood

1. Installs Bubo from PyPI (`uv tool install bubo`).
2. (hosted, optional) Best-effort installs Codex + Superpowers; authenticates Codex
   with your key.
3. `bubo init` lays down the workspace + agent profile + `code-reviewer` skill.
4. Writes `config/env.toml` from your inputs — `provider = "github"`, the PR's
   repo as the single project, and `[poller].target_merge_request_iid` set to the
   PR number, so the review is scoped to exactly this PR.
5. Runs the review; findings post inline via REST (the MCP path is skipped in CI).

## Publishing to the Marketplace

Once you've validated the action on a real PR: draft a GitHub release and tick
**"Publish this Action to the GitHub Marketplace"** (requires the `action.yml` at
the repo root — it's there — plus 2FA and accepting the Marketplace agreement).
Until then, consumers can use it by SHA/tag (`uses: mountainowl/bubo@<ref>`).
