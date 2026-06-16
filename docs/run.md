# Run

To review the current checkout once, run your configured agent directly with a
task (Codex is the bundled default; no GitLab interaction beyond the agent's
own MCP calls):

```sh
codex --ask-for-approval never exec --profile bubo --skip-git-repo-check \
  "Review the current changes."
```

The GitLab poller, one cycle:

```sh
uv run bubo-poller
```

For continuous operation, schedule it under cron or a systemd timer — there's
deliberately no daemon mode. Each invocation processes up to
`max_merge_requests_per_poll` MRs and exits. See [operate.md](operate.md)
for ready-to-copy cron/systemd templates.

## MCP server (`bubo-mcp`)

Bubo ships an MCP server with read-only metrics tools and a one-shot
`review_change` trigger, over stdio or HTTP. Full setup and the three deployment
patterns are on the **[MCP server](mcp.md)** page.

## How the GitHub provider talks to GitHub

The poller is provider-agnostic: a single `ScmProvider` abstraction
(see `src/bubo/scm/`) drives both GitLab and GitHub. Two
GitHub-specific mechanics are worth knowing:

- **Inline-comment posting** goes through a GitHub MCP server. The MCP
  tool name varies between server implementations; override it with the
  `BUBO_GITHUB_MCP_TOOL` environment variable. If the MCP call fails or
  the tool is missing, the poster falls back to the GitHub REST API for
  the same operation.
- **Thread resolution** (used by `--sync-outcomes`) is read via GitHub's
  GraphQL `reviewThreads` API, so resolved/unresolved counts reflect the
  actual review-thread state. If GraphQL is unavailable or the comment's
  thread can't be located, sync falls back to a resolution-blind REST
  path that still records posted/deleted/replied transitions.
