# Automated AI Based Code Reviewer

[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-3776ab?logo=python&logoColor=white)](pyproject.toml)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-2f3542)](pyproject.toml)
[![CI](https://github.com/mountainowl/ai-code-review/actions/workflows/ci.yml/badge.svg)](https://github.com/mountainowl/ai-code-review/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/mountainowl/ai-code-review/badge)](https://scorecard.dev/viewer/?uri=github.com/mountainowl/ai-code-review)
[![OpenTelemetry](https://img.shields.io/badge/metrics-OpenTelemetry-4f62ad)](#telemetry)
[![License: MIT](https://img.shields.io/badge/license-MIT-111827)](LICENSE)

Evidence-backed LLM code review for merge requests. Watches GitLab MRs, runs a
structured agent review, and posts only actionable findings as inline review
threads — no chatbot noise, no praise, no summaries.

![LLM Reviewer hero](docs/images/llm-reviewer-hero.png)

---

## Table of contents

1. [Example output](#example-output)
2. [Prerequisites](#prerequisites)
3. [Install](#install)
4. [Configure](#configure)
5. [Run](#run)
6. [How it works](#how-it-works)
7. [Configuration reference](#configuration-reference)
8. [Operate](#operate)
9. [Telemetry](#telemetry)
10. [Status and roadmap](#status-and-roadmap)
11. [Security](#security)
12. [Bot avatar](#bot-avatar)
13. [Community](#community)

---

## Example output

The bot posts inline review threads in a fixed shape — `Issue` / `Impact` /
`Evidence` / `Fix` / `Confidence`:

```text
Issue: HS256 JWT fallback is skipped when Cognito URL construction fails.
Impact: Valid local/shared-secret JWT requests return 500 instead of authenticating.
Evidence: The changed interceptor rethrows InvalidAwsUrlException before fallback runs.
Fix: Treat Cognito validation construction failures as failed Cognito auth when fallback is allowed.
Confidence: 0.94
```

Real (sanitized) inline findings on GitLab MRs:

![Sanitized inline finding — data primer](docs/images/gitlab-mr-review-data-primer.png)

![Sanitized inline finding — exception handler](docs/images/gitlab-mr-review-exception-handler.png)

More sanitized examples are in [docs/examples/README.md](docs/examples/README.md).
Demo GIF: [docs/media/llm-reviewer-demo.gif](docs/media/llm-reviewer-demo.gif).

---

## Prerequisites

Install these by hand on the review host before the package will run.
Nothing is bundled implicitly — if a tool below is missing, the corresponding
code path fails immediately.

### Runtime — required for any review

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

### Per-provider — required for the provider you enable in `[scm].provider`

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

### Credentials — required for any review

| Credential | What it does | Notes |
|---|---|---|
| **Bot user + token** | The bot account whose name appears on review threads/comments. | **GitLab:** token with `api` scope. **GitHub:** token with pull-request read+write. Create a dedicated bot account and add it to every reviewed project. |
| **LLM provider API key** | OpenAI, Anthropic, or another provider used by the review CLI. | Exported as `LLM_API_KEY` plus the provider-specific name matched from `[agents].llm_model`. |

### Optional

| Tool | Needed when |
|---|---|
| **OpenTelemetry collector** | You set `[telemetry].enabled = true`. Receives OTLP/gRPC metrics + spans on the configured endpoint. |
| **systemd or cron** | You want the poller to run on a schedule beyond a one-shot invocation. |

### Verify the install

`scripts/install-package.sh` only checks for `uv`; the other prerequisites
are runtime-resolved. Run this one-liner after install to confirm everything
the worker shells out to is on `PATH` — a missing tool here is the most
common cause of a first-cycle worker failure:

```sh
for bin in uv python3 git glab gh codex claude github-mcp-server mcp-gitlab; do
  printf '%-20s %s\n' "$bin" "$(command -v "$bin" 2>/dev/null || echo MISSING)"
done
```

You only need the tools for the providers and agents you've actually enabled
in `config/env.toml` — `MISSING` on the others is fine.

---

## Install

Clone the repo and install in place. The install needs the bundled
prompt, skill, config template, wrapper scripts, and deployment templates that ship with the checkout — pip-only installs are not supported.

```sh
git clone https://github.com/mountainowl/ai-code-review.git
cd ai-code-review
./scripts/install-package.sh
```

For a remote host:

```sh
./scripts/deploy-package.sh user@host
# or
./scripts/deploy-package.sh user@host --root /opt/llm-reviewer --sudo
```

For local development:

```sh
uv sync --dev
uv run pytest
```

---

## Configure

Copy the example config and edit it locally — `config/env.toml` is gitignored
and holds your tokens:

```sh
cp config/env.example.toml config/env.toml
```

Minimum changes to get a first review running:

```toml
[gitlab]
token = "glpat-..."          # api scope

[agents]
llm_api_key = "..."          # your LLM provider key
llm_model = "gpt-5.5"        # match what your CLI is configured for

[[projects]]
path = "your-group/your-repo"
enabled = true
```

Keep `[review].dry_run = true` (the default) until your first real review
output looks right — the poller will plan findings without posting comments.

### GitLab bot setup

1. Create a bot user, for example `llm-reviewer`.
2. Add it to every target GitLab project with permission to read MRs and
   create discussions.
3. Create a token with `api` scope.
4. Put the token in ignored `config/env.toml` under `[gitlab].token`.
5. List the projects under `[[projects]]` in the same file.

---

## Run

One-off review of the current checkout (manual; no GitLab interaction beyond
the agent's own MCP calls):

```sh
uv run code-review-codex "Review the current changes."
```

The GitLab poller (one cycle):

```sh
uv run mr-review-poller
```

Schedule it via cron or a systemd timer for continuous operation — there is
deliberately no daemon mode. Each invocation processes up to
`max_merge_requests_per_poll` MRs and exits.

### MCP interface (`mcp-llm-reviewer`)

llm-reviewer ships its own MCP server with **two interfaces** — a metrics
side for inspecting review state, and a review side for triggering a
fresh review by URL or `(provider, project, number)`.

**Metrics interface — read-only, against SQLite:**

| Tool | Returns |
|---|---|
| `health` | `{status, last_status, last_updated_at, age_seconds}` — same semantics as `mr-review-poller --health`. |
| `list_recent_reviews` | Recent `reviewed_mrs` rows newest-first, with optional `status` / `project` / `limit` filters. |
| `get_review` | One review row by `(project, iid[, sha])`; resolves to the latest SHA when unspecified. |
| `get_findings` | Per-finding rows (file, line, severity, category, confidence, posted body, discussion id). |
| `get_finding_outcomes` | Resolution state populated by `--sync-outcomes` (resolved / disputed / merged_unresolved / …). |
| `get_metrics` | Aggregated counts + token / cost sums for a `since_hours` window, optionally filtered by project. |

**Review interface — trigger a one-shot review:**

| Tool | Args | Returns |
|---|---|---|
| `review_change` | `url=…` **or** `provider={gitlab,github,auto}` + `project=…` + `number=…`; optional `timeout_seconds`. | `{provider, project, number, exit_code, duration_seconds, findings, raw_output}`. |

`provider="auto"` (the default) infers the provider from the URL when one is
given, otherwise falls back to `[scm].provider` in `config/env.toml`.
`review_change` blocks until the underlying `code-review-codex` subprocess
completes — set the client-side `tool_timeout_sec` accordingly. **MCP-triggered
reviews return findings inline; they do not write to `reviewed_mrs`**, so
they will not show up in the metrics tools. Use the poller for state-tracked
reviews.

#### Three deployment patterns

The server supports two transports — stdio (default) and HTTP+SSE with
bearer-token auth — selected via `[mcp_server].transport` in
`config/env.toml`. Pick the pattern that matches where Codex and the
reviewer actually live.

**Pattern 1 — same host (laptop runs Codex *and* the reviewer):**

```toml
# ~/.codex/config.toml
[mcp_servers.llm-reviewer]
command = "/absolute/path/to/llm-reviewer/bin/mcp-llm-reviewer"
args    = []
startup_timeout_sec = 20
tool_timeout_sec    = 1800   # ≥ [review].timeout_seconds for review_change
```

No reviewer-side config change needed — `[mcp_server].transport` defaults
to `"stdio"`. `~/` expansion is supported, so a `~/llm-reviewer/...` path
works across machines that install to the same per-user location.

**Pattern 2 — remote via SSH (laptop runs Codex; server runs the reviewer + holds the SQLite):**

```toml
# ~/.codex/config.toml
[mcp_servers.llm-reviewer]
command = "ssh"
args = [
    "-T",                                      # no pty; keeps stdout clean for MCP framing
    "-o", "ServerAliveInterval=30",            # keeps long review_change calls alive across NAT
    "llm-reviewer.example.com",                # ssh_config Host alias, or user@host
    "/opt/llm-reviewer/bin/mcp-llm-reviewer",  # absolute path on the server
]
startup_timeout_sec = 30
tool_timeout_sec    = 1800
```

Operator prerequisites: key-based SSH (interactive password breaks the
stdio loop), the install root readable by the SSH user, and no
MOTD/banner noise on stdout (set `PrintMotd no` server-side or
`LogLevel QUIET` client-side). Zero new code — SSH multiplexes the MCP
stdio over the wire.

**Pattern 3 — remote over HTTP+bearer (multi-tenant or org-wide):**

On the reviewer host, set `config/env.toml`:

```toml
[mcp_server]
transport    = "http"
host         = "0.0.0.0"                     # or a specific interface
port         = 8765
bearer_token = "${LLM_REVIEWER_MCP_TOKEN}"   # generate: openssl rand -hex 32
```

Then run `bin/mcp-llm-reviewer` (e.g. under a systemd unit). On the client:

```toml
# ~/.codex/config.toml — exact key for HTTP MCP servers varies across
# Codex versions; check `codex --help` if these names look wrong.
[mcp_servers.llm-reviewer]
url           = "https://reviewer.example.com/mcp"
bearer_token  = "..."                        # matches LLM_REVIEWER_MCP_TOKEN
startup_timeout_sec = 30
tool_timeout_sec    = 1800
```

The server enforces `Authorization: Bearer <token>` on every request and
returns 401 otherwise. **The server does not terminate TLS** — bind to
`127.0.0.1` and front it with nginx/caddy, expose only over a VPN, or
accept that the token traverses the network in clear text.

#### Upstream wrappers

There are also two **upstream** MCP-server wrappers — `bin/mcp-upstream-gitlab`
and `bin/mcp-upstream-github` — that locate the third-party GitLab / GitHub
MCP server on `PATH` and exec it with `config/env.toml` tokens injected. The
poster path uses these to create inline review threads; you can also point
Codex at them directly if you want a chat-driven session with the same
MCP surface the reviewer uses.

---

## How it works

```mermaid
flowchart TB
    A["Open merge requests"]
    B["Poller<br/>+ SQLite state"]
    C["Forked review worker"]
    D["Agent review skill<br/>(Codex / Claude)"]
    E["Inline review<br/>discussions"]
    F["OpenTelemetry<br/>+ outcome sync"]

    A --> B --> C --> D --> E --> F
```

1. The poller lists open MRs for each configured project, skipping any it has
   already reviewed at the current head SHA.
2. For each eligible MR it forks a worker. The worker checks out the MR diff,
   runs the agent review skill, and parses structured findings.
3. Each finding is mapped to a changed line in the MR diff and posted as an
   inline GitLab review thread (or stored as a "planned" finding if
   `dry_run` is on).
4. SQLite records reviewed SHAs and posted-finding fingerprints so the bot
   does not spam the same MR or duplicate a comment.
5. `--sync-outcomes` later checks which posted findings were resolved,
   replied to, marked false-positive, deleted, or merged-unresolved.

The poller does not try to be a code-review brain. It orchestrates SCM access,
state, prompt rendering, posting, and metrics. The actual review logic lives
in the configured CLI skill.

---

## Configuration reference

Public defaults live in [`config/env.example.toml`](config/env.example.toml).
Copy it to ignored `config/env.toml` before running. Runtime config and
credentials live in that one TOML file.

<table>
  <thead>
    <tr>
      <th>Setting</th>
      <th>Default</th>
      <th>Purpose / impact</th>
    </tr>
  </thead>
  <tbody>
    <tr><th colspan="3"><code>[scm]</code></th></tr>
    <tr>
      <td><code>provider</code></td>
      <td><code>gitlab</code></td>
      <td>Source-control backend: <code>gitlab</code> or <code>github</code>. Selects which provider the poller drives. <code>gh-review-poller</code> forces <code>github</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[gitlab]</code></th></tr>
    <tr>
      <td><code>url</code></td>
      <td><code>https://gitlab.com</code></td>
      <td>Web host the poller reads MRs from. For self-hosted GitLab, keep <code>api_url</code> on the same host.</td>
    </tr>
    <tr>
      <td><code>api_url</code></td>
      <td><code>https://gitlab.com/api/v4</code></td>
      <td>API endpoint used by MCP tools inside the review agent.</td>
    </tr>
    <tr>
      <td><code>bot_username</code></td>
      <td><code>llm-reviewer</code></td>
      <td>Lets outcome sync separate bot comments from developer replies.</td>
    </tr>
    <tr>
      <td><code>denied_tools_regex</code></td>
      <td><code>^(delete_.*|merge_merge_request|push_files)$</code></td>
      <td>Blocks dangerous GitLab MCP tools even if the agent can see them.</td>
    </tr>
    <tr>
      <td><code>token</code></td>
      <td>unset</td>
      <td>GitLab token with <code>api</code> scope. Exported as <code>GITLAB_TOKEN</code>, <code>GITLAB_PERSONAL_ACCESS_TOKEN</code>, and <code>GLAB_TOKEN</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[github]</code></th></tr>
    <tr>
      <td><code>api_url</code></td>
      <td><code>https://api.github.com</code></td>
      <td>REST API base. Use <code>https://&lt;host&gt;/api/v3</code> for GitHub Enterprise Server.</td>
    </tr>
    <tr>
      <td><code>bot_username</code></td>
      <td><code>llm-reviewer</code></td>
      <td>Lets outcome sync separate bot comments from developer replies.</td>
    </tr>
    <tr>
      <td><code>token</code></td>
      <td>unset</td>
      <td>GitHub token with pull-request read+write. Exported as <code>GITHUB_TOKEN</code>, <code>GITHUB_PERSONAL_ACCESS_TOKEN</code>, and <code>GH_TOKEN</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[review]</code></th></tr>
    <tr>
      <td><code>dry_run</code></td>
      <td><code>true</code></td>
      <td>Stores planned findings without posting comments. Set <code>false</code> after test reviews look right.</td>
    </tr>
    <tr>
      <td><code>max_merge_requests_per_poll</code></td>
      <td><code>8</code></td>
      <td>Caps how many MRs one poll cycle queues. Higher values can fork more workers at once.</td>
    </tr>
    <tr>
      <td><code>max_findings_per_merge_request</code></td>
      <td><code>8</code></td>
      <td>Caps findings per MR and fills <code>{{MAX_FINDINGS_PER_REVIEW}}</code> in the prompt.</td>
    </tr>
    <tr>
      <td><code>timeout_seconds</code></td>
      <td><code>1800</code></td>
      <td>Kills a review worker that runs too long.</td>
    </tr>
    <tr>
      <td><code>min_confidence</code></td>
      <td><code>0.85</code></td>
      <td>Floor for the LLM's per-finding confidence (0.0–1.0). Findings below this score are dropped before posting or planning. Inclusive on the high side.</td>
    </tr>
    <tr>
      <td><code>allowed_kinds</code></td>
      <td><code>[]</code></td>
      <td>Whitelist of finding kinds to post. A finding is kept if its <code>severity</code>, <code>category</code>, or <code>type</code> appears here (case-insensitive). Empty list = no kind filter — post everything that clears <code>min_confidence</code>. Common values: <code>"blocking"</code>, <code>"non-blocking"</code>, <code>"security"</code>, <code>"correctness"</code>, <code>"performance"</code>, <code>"issue"</code>, <code>"suggestion"</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[poller]</code></th></tr>
    <tr>
      <td><code>state_dir</code></td>
      <td><code>var</code></td>
      <td>Stores SQLite state, logs, reports, worktrees, and rendered prompts.</td>
    </tr>
    <tr>
      <td><code>interval_seconds</code></td>
      <td><code>900</code></td>
      <td>Suggested wait for long-running poll loops. Cron/systemd can use another interval.</td>
    </tr>
    <tr>
      <td><code>target_merge_request_iid</code></td>
      <td>unset</td>
      <td>Temporary single-MR filter. Leave unset in production.</td>
    </tr>
    <tr><th colspan="3"><code>[agents]</code></th></tr>
    <tr>
      <td><code>prompt_file</code></td>
      <td><code>prompts/00-meta.md</code></td>
      <td>Meta prompt rendered before each review.</td>
    </tr>
    <tr>
      <td><code>llm_model</code></td>
      <td><code>gpt-5.5</code></td>
      <td>Model passed to the review wrapper. Keep telemetry pricing aligned for cost metrics.</td>
    </tr>
    <tr>
      <td><code>llm_api_key</code></td>
      <td>unset</td>
      <td>LLM provider key. Exported as <code>LLM_API_KEY</code>, <code>OPENAI_API_KEY</code>, <code>ANTHROPIC_API_KEY</code>, and <code>QWEN_API_KEY</code>.</td>
    </tr>
    <tr>
      <td><code>reasoning_effort</code></td>
      <td><code>medium</code></td>
      <td>Review reasoning level. Higher values can cost more and run longer.</td>
    </tr>
    <tr>
      <td><code>dry_run</code></td>
      <td><code>true</code></td>
      <td>Dry-run default for the manual <code>code-review-codex</code> wrapper, separate from <code>[review].dry_run</code> which controls poller posting.</td>
    </tr>
    <tr>
      <td><code>codex_profile</code></td>
      <td><code>llm-reviewer</code></td>
      <td>Codex profile used by the Codex wrapper.</td>
    </tr>
    <tr>
      <td><code>codex_sandbox</code></td>
      <td><code>read-only</code></td>
      <td>Filesystem access passed to Codex review runs.</td>
    </tr>
    <tr><th colspan="3"><code>[telemetry]</code></th></tr>
    <tr>
      <td><code>enabled</code></td>
      <td><code>false</code></td>
      <td>Sends OTel metrics and spans when enabled. SQLite state is still written either way.</td>
    </tr>
    <tr>
      <td><code>service_name</code></td>
      <td><code>llm-reviewer</code></td>
      <td>Service name shown in the OTel backend.</td>
    </tr>
    <tr>
      <td><code>environment</code></td>
      <td><code>prod</code></td>
      <td>Environment label for dashboards, such as <code>dev</code>, <code>staging</code>, or <code>prod</code>.</td>
    </tr>
    <tr>
      <td><code>otlp_endpoint</code></td>
      <td><code>http://127.0.0.1:4317</code></td>
      <td>Collector endpoint for metrics and traces.</td>
    </tr>
    <tr>
      <td><code>otlp_protocol</code></td>
      <td><code>grpc</code></td>
      <td>OTLP transport. Only <code>grpc</code> is supported today.</td>
    </tr>
    <tr>
      <td><code>export_interval_seconds</code></td>
      <td><code>30</code></td>
      <td>Metric export interval. Lower values make dashboards fresher.</td>
    </tr>
    <tr>
      <td><code>emit_finding_events</code></td>
      <td><code>true</code></td>
      <td>Emits finding lifecycle metrics like planned, posted, skipped, and resolved.</td>
    </tr>
    <tr>
      <td><code>emit_outcome_sync</code></td>
      <td><code>true</code></td>
      <td>Emits metrics when outcome sync checks posted finding status.</td>
    </tr>
    <tr>
      <td><code>input_per_1m</code></td>
      <td><code>5.0</code></td>
      <td>Estimated input-token price per million tokens for cost metrics.</td>
    </tr>
    <tr>
      <td><code>output_per_1m</code></td>
      <td><code>30.0</code></td>
      <td>Estimated output-token price per million tokens for cost metrics.</td>
    </tr>
    <tr>
      <td><code>cached_input_per_1m</code></td>
      <td><code>0.5</code></td>
      <td>Estimated cached-input price per million tokens for cost metrics.</td>
    </tr>
    <tr><th colspan="3"><code>[[projects]]</code></th></tr>
    <tr>
      <td><code>path</code></td>
      <td>sample repos</td>
      <td>GitLab project path to poll, for example <code>group/repo</code>.</td>
    </tr>
    <tr>
      <td><code>enabled</code></td>
      <td><code>true</code></td>
      <td>Turns polling for that project on or off.</td>
    </tr>
  </tbody>
</table>

---

## Operate

### Deploy to a host

```sh
./scripts/deploy-package.sh user@host
# installs under $HOME/.local/share/llm-reviewer, runs uv sync --locked --no-dev
```

Custom root or sudo install:

```sh
./scripts/deploy-package.sh user@host --root /opt/llm-reviewer --sudo
./scripts/deploy-package.sh user@host --install-agent-config   # adds Codex/Claude config templates
```

For a host-local install after copying the checkout yourself:

```sh
./scripts/install-package.sh
```

The wrappers in `bin/` infer the install root from their own location and
load `config/env.toml`. No activation step.

### Schedule the poller

> **`install-package.sh` does NOT install cron entries or systemd units.**
> Scheduling is a separate, deliberate step — operators run llm-reviewer
> under different scheduling regimes (cron, systemd, Kubernetes
> CronJob, Nomad, …) and the install path stays scheduler-agnostic.

The install ships three ready-to-copy templates under
`deploy/templates/`. Pick **one** of the two paths below; both achieve
the same cadence (poll every 15 min, sync outcomes hourly, health probe
every 5 min).

**Cron** (`deploy/templates/llm-reviewer.cron`) — the template is a
distro-style drop-in file. Copy it into whichever location your cron
implementation reads (commonly the system cron drop directory on
Debian/Ubuntu/RHEL; the user crontab on macOS; the operator's choice on
custom builds):

```sh
# As the install user (or root):
CRON_DROP_DIR="$(your distro's cron drop directory)"
sudo install -m 0644 \
  "$LLM_CODE_REVIEW_ROOT/deploy/templates/llm-reviewer.cron" \
  "$CRON_DROP_DIR/llm-reviewer"
# Then edit the installed file to point LLM_CODE_REVIEW_ROOT at your install path.
```

The template's three lines fire `mr-review-poller` (poll cycle),
`mr-review-poller --sync-outcomes` (hourly outcome grading), and
`mr-review-poller --health` (liveness probe) at different cadences. Each
invocation is a single exit — there is no daemon mode, so tight
intervals are safe.

**systemd** (`deploy/templates/llm-reviewer.{service,timer}`):

```sh
sudo cp $LLM_CODE_REVIEW_ROOT/deploy/templates/llm-reviewer.service /etc/systemd/system/
sudo cp $LLM_CODE_REVIEW_ROOT/deploy/templates/llm-reviewer.timer   /etc/systemd/system/
# Edit the .service file to point LLM_CODE_REVIEW_ROOT and credential paths
# at your install, then:
sudo systemctl daemon-reload
sudo systemctl enable --now llm-reviewer.timer
```

The service file uses `LoadCredential=` to inject secrets from
`/etc/llm-reviewer/credentials/`, so tokens stay off-disk in
`config/env.toml`. Pair with TOML env interpolation in the config:

```toml
[gitlab]
token = "${GITLAB_TOKEN}"
[agents]
llm_api_key = "${LLM_API_KEY}"
```

### Outcome sync

`--sync-outcomes` grades posted findings against current SCM state. It
runs from the same cron line the install template provides; you don't
need to invoke it by hand once scheduling is set up.

```sh
bin/mr-review-poller --sync-outcomes
```

Records whether each finding was resolved, left unresolved after merge,
deleted, replied to, marked disputed, marked false-positive, or marked
duplicate — feeding the `llm_review.findings{status=…}` counter
described in [Telemetry](#telemetry).

### Backfill — one-shot, not a cron job

The backfill commands import bot comments that **already exist on the
SCM** into local SQLite. Use them when:

- You just deployed against a project where the bot has historically
  posted from another install.
- You reset `var/state/reviewer.sqlite` (test, rebuild, host migration)
  and need the per-finding metrics to reflect history, not just go-forward.

They are **deliberately not on the cron schedule.** Each run scans every
MR/PR updated since the cutoff — fine for a one-shot recovery, wasteful
to repeat every 15 minutes. Run them once, then let `--sync-outcomes`
take over for go-forward grading.

```sh
# GitLab
bin/mr-review-poller --backfill-gitlab-bot-comments-since 2026-05-25T00:00:00Z

# GitHub
bin/gh-review-poller --backfill-github-bot-comments-since 2026-05-25T00:00:00Z

# Then grade the imported rows once:
bin/mr-review-poller --sync-outcomes
```

Both backfill commands are idempotent — a comment already in SQLite is
upserted, not duplicated — so re-running with a different cutoff is
safe.

---

## Telemetry

LLM Reviewer emits OpenTelemetry metrics and traces so dashboard rollups
stay outside the poller. All metrics are namespaced `llm_review.*` and
registered in `src/llm_reviewer/telemetry/metrics.py`.

### Emitted metrics

| Metric | Type | What it counts | Key attributes | Example sample (OTLP) |
|---|---|---|---|---|
| `llm_review.runs` | counter | One increment per completed review run (a single MR/PR worker exiting). | `repo`, `model`, `status` (`success`/`no_findings`/`failed`/`skipped`), `review_mode` (`poller`/`manual`), `dry_run` | `llm_review.runs{repo="g/r", model="gpt-5.5", status="success", review_mode="poller", dry_run="false"} 1` |
| `llm_review.findings` | counter | One increment per finding lifecycle event — initial post AND every outcome transition picked up by `--sync-outcomes`. | `repo`, `status` (`planned`/`posted`/`skipped`/`pending_external_id`/`resolved`/`disputed`/`false_positive`/`duplicate`/`deleted`/`developer_replied`), `dry_run`, `finding_type`, `severity`, `category` | `llm_review.findings{repo="g/r", status="posted", severity="blocking", category="correctness"} 1` |
| `llm_review.tokens` | counter | LLM token consumption per review, split into four streams. | `repo`, `model`, `status`, `review_mode`, `dry_run`, `operation` (`input`/`output`/`cached`/`total`) | `llm_review.tokens{repo="g/r", model="gpt-5.5", operation="total"} 65926` |
| `llm_review.cost.usd` | counter | Estimated provider cost in USD per review, summed from the configured `[telemetry.pricing.*]` rates. | `repo`, `model`, `status`, `review_mode`, `dry_run` | `llm_review.cost.usd{repo="g/r", model="gpt-5.5"} 0.32963` |
| `llm_review.failures` | counter | One increment per failed pipeline stage. | `repo`, `error_type` (Python exception class), `operation` (`review` for a failed worker, `outcome_sync` for a failed `--sync-outcomes` fetch) | `llm_review.failures{repo="g/r", operation="outcome_sync", error_type="HTTPError"} 1` |
| `llm_review.latency.review_seconds` | histogram | Per-review wall-clock from worker start to finish. | `repo`, `model`, `status`, `review_mode`, `dry_run` | `llm_review.latency.review_seconds_sum{repo="g/r"} 412.3`<br>`llm_review.latency.review_seconds_count{repo="g/r"} 3` |
| `llm_review.latency.queue_seconds` | histogram | Time from when a job is written to the queue to when a worker picks it up — your saturation signal. | `repo` | `llm_review.latency.queue_seconds_sum{repo="g/r"} 6.1`<br>`llm_review.latency.queue_seconds_count{repo="g/r"} 3` |

Two switches in `config/env.toml` let you trim emission cost:

- `[telemetry].emit_finding_events` — drop the per-finding counter
  entirely if you only care about run-level rollups.
- `[telemetry].emit_outcome_sync` — drop the outcome-derived increments on
  `llm_review.findings` (`resolved`/`disputed`/`false_positive`/etc.) and
  keep only the initial-post events.

### Common dashboards

- **Throughput**: `rate(llm_review.runs[5m])` grouped by `repo` + `status`.
- **Saturation**: `histogram_quantile(0.95, llm_review.latency.queue_seconds_bucket)` — if this climbs, raise `max_merge_requests_per_poll`.
- **Cost**: `sum(rate(llm_review.cost.usd[1d])) by (repo, model)`.
- **Quality / ROI**: ratio of `llm_review.findings{status="resolved"}` over `llm_review.findings{status="posted"}`, grouped by `severity`.
- **Reliability**: `rate(llm_review.failures[5m])` broken out by `operation`.

### Cardinality discipline

Metric attributes are kept low-cardinality on purpose. MR IID, SHA, file
path, line number, fingerprint, and discussion ID live in **SQLite or
span events only**, never as metric labels — so the dashboard backend
doesn't melt as the queue accelerates. Spans carry the full per-MR
context for trace-level drilldown.

---

## Status and roadmap

- **GitLab posting via polling** — production path. Stable.
- **GitHub posting via polling** — supported, at outcome-metric parity with
  GitLab. Set `[scm].provider = "github"` (or run `gh-review-poller`, which
  forces it). The poller is provider-agnostic: a single :class:`ScmProvider`
  abstraction drives both backends. Inline-comment posting goes through a
  GitHub MCP server with a REST fallback (the MCP tool name varies between
  server implementations and is overrideable via
  `LLM_REVIEWER_GITHUB_MCP_TOOL`). Thread *resolution* is read via GitHub's
  GraphQL `reviewThreads` API, so `--sync-outcomes` reports real
  resolved/unresolved counts (with a resolution-blind REST fallback if
  GraphQL is unavailable or the comment's thread can't be located).
- **Webhook-driven triggering** — not implemented; polling is the only path.
- **pip-only install** — not supported. The install needs the bundled prompt,
  skill, config template, wrapper scripts, and deployment templates that ship
  with the checkout.

Review execution is intentionally outside CI/CD. Run it as a poller beside your existing pipelines.

---

## Security

- `config/env.toml` is gitignored and holds tokens. **Do not print or commit
  real values from it.**
- Review-agent stdout is redacted (`GITLAB_TOKEN=`, `OPENAI_API_KEY=`, `glpat-…`,
  `sk-…`, and credentialed Git URLs) before being written to reports, logs, or
  the database error column.
- The reviewer subprocess is launched with a strict env allowlist (see
  `REVIEWER_ENV_ALLOWLIST` in `src/llm_reviewer/poller.py`) — host secrets are
  not passed wholesale into the LLM agent.
- Report vulnerabilities per [`SECURITY.md`](SECURITY.md).

---

## Bot avatar

Upload [`assets/llm-reviewer.png`](assets/llm-reviewer.png) as the GitLab (or
future GitHub) bot avatar.

![LLM Reviewer avatar preview](docs/images/llm-reviewer-avatar-preview.png)

---

## Community

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Support](SUPPORT.md)
- [Code of conduct](CODE_OF_CONDUCT.md)
