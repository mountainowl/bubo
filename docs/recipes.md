# Recipes

Copy-paste setups you can follow top to bottom — replace the values in
`< >` with your own. The Codex recipes are the bundled default path; the
[Claude](#claude) section covers pointing Bubo at a different agent.

!!! tip "Which agent runs the review"
    Bubo runs the review through whatever agent CLI you set in
    `[agents].reviewer_command`: the poller appends the rendered review prompt
    to that command and reads a JSON array of findings from its stdout.
    **Codex is the bundled default** (`bin/bubo-codex`, so you don't set
    `reviewer_command` at all); pointing it at **Claude** (`claude -p`) is
    just as valid — see [Claude](#claude). The worked examples below use
    Codex; everything except `reviewer_command` and the agent's own
    setup is identical for any agent.

---

## Codex (OpenAI) — GitLab

The fastest path to a first review on a GitLab merge request.

### 1. Prerequisites

Install these once and make sure they're on `PATH` (see
[prerequisites](prerequisites.md) for platform-specific commands):

- **uv**, **Python 3.14+**, **Git**
- **Codex CLI**, authenticated to OpenAI
- **Superpowers + the `code-reviewer` skill** in your Codex config
- **glab** (GitLab CLI) — for cloning/fetching MRs

### 2. Install Bubo

```sh
uv tool install git+https://github.com/mountainowl/bubo@v0.8.0
bubo init           # writes ~/.codex/config.toml (with [profiles.bubo]),
                    # ~/.claude/settings.json, the workspace, and the SQLite DB
```

### 3. Configure

`bubo init` seeds the config at
`~/.local/share/bubo/config/env.toml`. Open it and fill in the minimum:

```toml
[scm]
provider = "gitlab"

[gitlab]
token = "<glpat-your-token>"        # GitLab token with `api` scope

[agents]
llm_model = "gpt-5.5"               # any model your Codex profile drives
llm_api_key = "<sk-your-openai-key>"
llm_api_key_env = "OPENAI_API_KEY"  # the env var the OpenAI/Codex CLI reads

[[projects]]
path = "<your-group>/<your-repo>"   # repos Bubo should watch
enabled = true
```

### 4. Verify and run

```sh
bubo doctor          # checks workspace, config, DB, and the Codex profile block
bubo-poller          # one poll cycle; reviews open MRs, then exits
```

The first run is **dry-run by default** (`[review].dry_run = true`) — Bubo
plans findings and writes them to SQLite but posts nothing. Read a
transcript under `~/.local/share/bubo/var/reports/`, and when the output
looks right, flip `[review].dry_run = false` to start posting inline
review threads.

### 5. Schedule it (optional)

`bubo init` rendered ready-to-install cron + systemd units under
`~/.local/share/bubo/deploy/templates/`. See [operate](operate.md) for
the install steps.

---

## Codex (OpenAI) — GitHub

Identical to the GitLab recipe with three changes:

- Install the **`gh` CLI** instead of `glab`, authenticated to GitHub.
- Use a **GitHub token** with pull-request read+write.
- Set the provider to GitHub:

```toml
[scm]
provider = "github"

[github]
token = "<ghp-your-token>"

[agents]
llm_model = "gpt-5.5"
llm_api_key = "<sk-your-openai-key>"
llm_api_key_env = "OPENAI_API_KEY"

[[projects]]
path = "<owner>/<repo>"
enabled = true
```

Run with `bubo-gh-poller` (it forces the GitHub provider) or with
`bubo-poller` once `[scm].provider = "github"` is set.

---

## Another OpenAI-compatible model

Bubo doesn't hardcode providers — `llm_api_key_env` names the variable
your CLI reads. To point Codex at a different model, change the model
in your Codex profile (`~/.codex/config.toml`, `[profiles.bubo]`) and set:

```toml
[agents]
llm_model = "<your-model>"
llm_api_key = "<your-key>"
llm_api_key_env = "<THE_ENV_VAR_YOUR_CLI_READS>"   # e.g. OPENAI_API_KEY
```

---

## Claude

To run reviews with Claude instead of Codex, point `[agents].reviewer_command`
at a headless Claude invocation. The poller appends the rendered review prompt
as the final argument, so Claude gets the same instructions Codex does and
prints the findings JSON to stdout — everything else in your `[scm]` /
`[gitlab]` / `[[projects]]` config is unchanged.

```toml
[agents]
# Run the review through Claude Code in headless (-p / print) mode instead of
# the bundled Codex wrapper. Keep the DEFAULT text output — Bubo parses a JSON
# array from stdout, so do NOT add `--output-format json` (that wraps the array
# in an envelope Bubo won't read).
reviewer_command = ["claude", "-p", "--allowedTools", "Read,Bash"]

llm_model = "<your-claude-model>"      # used for cost/metric labels
llm_api_key = "<your-anthropic-key>"
llm_api_key_env = "ANTHROPIC_API_KEY"  # the env var the Claude CLI reads
```

Because Claude is bring-your-own-command (there's no bundled wrapper like
`bin/bubo-codex`), confirm it can do the review **non-interactively** before
going live:

- **Tools / permissions.** The review reads the diff and the MR/PR through
  MCP, so Claude must be allowed to use those tools without prompting — extend
  `--allowedTools` (e.g. add the git/GitLab MCP tool names) or pick a
  `--permission-mode` that matches your security posture.
- **MCP servers.** Register the same git + GitLab/GitHub MCP servers the Codex
  profile uses — `claude mcp add …`, a project `.mcp.json`, or
  `--mcp-config <file>`.
- **Skills.** Headless `-p` mode does not load slash-command skills like
  `/code-review`; Bubo passes the full rendered review prompt as the argument
  instead, so none is needed.

Then verify exactly as you would for Codex: keep `[review].dry_run = true`, run
`bubo-poller` once, and read the transcript under `var/reports/` to confirm
Claude's output parses into findings before you flip `dry_run` to `false`.
