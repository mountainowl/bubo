# Prerequisites

A short list of tools to install on the review host before Bubo runs.
Nothing is bundled implicitly — a missing tool fails its code path right away.

## Runtime — required for any review

Needed for every review path, whatever provider you use. Copy-paste a block:

**macOS**

```sh
# uv — project + dependency manager. Every CLI script invokes `uv run`.
curl -LsSf https://astral.sh/uv/install.sh | sh

# Python 3.14+ — runtime. Managed by uv so the project version stays pinned.
uv python install 3.14

# Git CLI — the worker runs `git fetch` / `git checkout` against change refs.
brew install git

# Codex CLI or Claude CLI — the configured review agent. Install one and
# authenticate to your LLM provider:
#   Codex:       https://github.com/openai/codex
#   Claude Code: https://www.anthropic.com/claude-code

# Superpowers + `code-reviewer` skill — the review prompt invokes
# /using-superpowers and the $code-reviewer skill. Without Superpowers
# configured in your CLI the agent will not run the review contract.
# Install Superpowers into your Codex/Claude config. The bundled skill
# assets live under plugins/superpowers/ and skills/code-reviewer/.
#   https://github.com/obra/superpowers
```

**Linux (Debian/Ubuntu)**

```sh
# uv — same one-liner across platforms.
curl -LsSf https://astral.sh/uv/install.sh | sh

# Python 3.14+ — uv manages it; distro package is a fallback.
uv python install 3.14

# Git CLI — distro package.
sudo apt install -y git

# Codex CLI / Claude CLI / Superpowers — see the macOS block; install steps
# are platform-agnostic (npm / shell installer / config file).
```

## Per-provider — required for the provider you enable in `[scm].provider`

| Provider | Tools | macOS | Linux |
|---|---|---|---|
| **GitLab** (`provider = "gitlab"`) | [`glab`](https://gitlab.com/gitlab-org/cli) (clones each MR) + a **GitLab MCP server** on `PATH` as `mcp-gitlab` / `gitlab-mcp` (posts inline threads). | `brew install glab` + `npm install -g @zereight/mcp-gitlab` | `sudo apt install glab` (or [other distros](https://gitlab.com/gitlab-org/cli#installation)) + `npm install -g @zereight/mcp-gitlab` |
| **GitHub** (`provider = "github"`) | [`gh`](https://cli.github.com/) (clones each PR) + a **GitHub MCP server** on `PATH` as `github-mcp-server` / `mcp-github` / `gh-mcp-server` (posts inline review comments; falls back to REST if the MCP tool name differs). | `brew install gh` + install [github-mcp-server](https://github.com/github/github-mcp-server) (release binary or `go install`) | [`gh` apt setup](https://github.com/cli/cli/blob/trunk/docs/install_linux.md) + install [github-mcp-server](https://github.com/github/github-mcp-server) (release binary or `go install`) |

**Authenticate each CLI once** so the `glab repo clone` / `gh repo clone`
paths can reach private repositories:

```sh
# GitLab
glab auth login              # paste a PAT or use the web flow
# GitHub
gh auth login                # web flow (recommended) or paste a PAT
```

These logins are separate from the bot token in `config/env.toml`: the CLI
tokens authorize cloning on the review machine; the bot token authorizes the
REST/MCP calls that read MRs/PRs and post comments.

## Credentials — required for any review

| Credential | What it does | Notes |
|---|---|---|
| **Bot user + token** | The bot account whose name shows on review threads/comments. | **GitLab:** token with `api` scope. **GitHub:** token with pull-request read+write. Use a dedicated bot account and add it to every reviewed project. |
| **LLM API key** | OpenAI, Anthropic, Gemini, or whatever model your review CLI runs. | Exported as `LLM_API_KEY` plus the operator-named variable in `[agents].llm_api_key_env` (e.g. `OPENAI_API_KEY`). |

## Optional

| Tool | Needed when |
|---|---|
| **OpenTelemetry collector** | You set `[telemetry].enabled = true`. Receives OTLP/gRPC metrics + spans on the configured endpoint. |
| **systemd or cron** | You want the poller to run on a schedule beyond a one-shot invocation. |

## Verify the install

`uv tool install` checks only `uv` itself; the rest resolve at the first poll.
`bubo doctor` covers the Python side (workspace, env.toml, DB, Codex profile)
but does NOT check that the external CLIs the worker shells out to are on
`PATH`. Run this after `bubo init` to catch a missing tool before the first
cycle — the most common cause of a first-cycle worker failure:

```sh
for bin in uv python3 git glab gh codex claude github-mcp-server mcp-gitlab; do
  printf '%-20s %s\n' "$bin" "$(command -v "$bin" 2>/dev/null || echo MISSING)"
done
```

You only need tools for the providers and agents you've enabled in
`config/env.toml` — `MISSING` on the rest is fine.
