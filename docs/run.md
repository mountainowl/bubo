# Run

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
`max_merge_requests_per_poll` MRs and exits. See [operate.md](operate.md)
for ready-to-copy cron/systemd templates.

## MCP interface (`mcp-llm-reviewer`)

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

### Three deployment patterns

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

### Upstream wrappers

There are also two **upstream** MCP-server wrappers — `bin/mcp-upstream-gitlab`
and `bin/mcp-upstream-github` — that locate the third-party GitLab / GitHub
MCP server on `PATH` and exec it with `config/env.toml` tokens injected. The
poster path uses these to create inline review threads; you can also point
Codex at them directly if you want a chat-driven session with the same
MCP surface the reviewer uses.

## How the GitHub provider talks to GitHub

The poller is provider-agnostic: a single `ScmProvider` abstraction
(see `src/llm_reviewer/scm/`) drives both GitLab and GitHub. The
GitHub-specific mechanics worth knowing:

- **Inline-comment posting** goes through a GitHub MCP server. The MCP
  tool name varies between server implementations and is overrideable
  via the `LLM_REVIEWER_GITHUB_MCP_TOOL` environment variable. If the
  MCP call fails or the tool is missing, the poster falls back to the
  GitHub REST API for the same operation.
- **Thread resolution** (used by `--sync-outcomes`) is read via GitHub's
  GraphQL `reviewThreads` API, so resolved/unresolved counts reflect the
  actual review-thread state. If GraphQL is unavailable or the comment's
  thread can't be located, sync falls back to a resolution-blind REST
  path that still records posted/deleted/replied transitions.
