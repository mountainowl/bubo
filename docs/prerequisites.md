# Prerequisites

Install these by hand on the review host before the package will run.
Nothing is bundled implicitly — if a tool below is missing, the corresponding
code path fails immediately.

## Runtime — required for any review

Required for any review path, regardless of provider. Copy-paste a block:

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

After installing the CLIs, **authenticate each one once** so the
`glab repo clone` / `gh repo clone` paths can reach private repositories:

```sh
# GitLab
glab auth login              # paste a PAT or use the web flow
# GitHub
gh auth login                # web flow (recommended) or paste a PAT
```

These authentications are independent of the bot token in `config/env.toml` —
the CLI tokens authorize the clone host on the review machine; the bot token
authorizes the REST/MCP calls that read MRs/PRs and post comments.

## Credentials — required for any review

| Credential | What it does | Notes |
|---|---|---|
| **Bot user + token** | The bot account whose name appears on review threads/comments. | **GitLab:** token with `api` scope. **GitHub:** token with pull-request read+write. Create a dedicated bot account and add it to every reviewed project. |
| **LLM provider API key** | OpenAI, Anthropic, or another provider used by the review CLI. | Exported as `LLM_API_KEY` plus the provider-specific name matched from `[agents].llm_model`. |

## Optional

| Tool | Needed when |
|---|---|
| **OpenTelemetry collector** | You set `[telemetry].enabled = true`. Receives OTLP/gRPC metrics + spans on the configured endpoint. |
| **systemd or cron** | You want the poller to run on a schedule beyond a one-shot invocation. |

## Verify the install

`uv tool install` only verifies `uv` itself; the other prerequisites
are runtime-resolved at first poll. `llm-reviewer doctor` checks the
Python-side install (workspace, env.toml, DB, Codex profile), but it
does NOT check that the external CLIs the worker shells out to are on
`PATH`. Run this one-liner after `llm-reviewer init` to catch a
missing tool before the first cycle — that's the most common cause
of a first-cycle worker failure:

```sh
for bin in uv python3 git glab gh codex claude github-mcp-server mcp-gitlab; do
  printf '%-20s %s\n' "$bin" "$(command -v "$bin" 2>/dev/null || echo MISSING)"
done
```

You only need the tools for the providers and agents you've actually enabled
in `config/env.toml` — `MISSING` on the others is fine.
