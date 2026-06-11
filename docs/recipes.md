# Recipes

Copy-paste setups you can follow top to bottom — replace the values in
`< >` with your own. Codex is the bundled default; the [Claude](#claude)
section covers reviewing with Claude instead.

!!! tip "Codex or Claude"
    Bubo runs the review through a small wrapper around your agent CLI.
    **Codex is bundled and works out of the box**, so the recipes below use
    it; you can review with **Claude** instead — see [Claude](#claude).

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

Run `bubo-poller` once `[scm].provider = "github"` is set, or force the
provider for a single run with `BUBO_PROVIDER=github bubo-poller`.

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

Bubo runs the review through a wrapper around your agent CLI, so Claude works
the same way as Codex — only the CLI it calls and the key it uses change.
Install the Claude CLI, then set Claude as your agent in `[agents]`:

```toml
[agents]
llm_model = "<your-claude-model>"
llm_api_key = "<your-anthropic-key>"
llm_api_key_env = "ANTHROPIC_API_KEY"   # the env var the Claude CLI reads
```

Codex is the agent that ships pre-wired, so reviewing with Claude needs one
extra step — having the wrapper call the Claude CLI for the review. Verify it
the same way as Codex: keep `[review].dry_run = true`, run `bubo-poller` once,
and read the transcript under `var/reports/` to confirm findings come through
before you flip `dry_run` to `false`.
