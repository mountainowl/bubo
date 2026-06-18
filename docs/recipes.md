# Recipes

Copy-paste setups, top to bottom — swap the `< >` placeholders for your own
values. Codex is the bundled default; [Claude](#claude) covers reviewing with
Claude instead.

!!! tip "Codex or Claude"
    Bubo runs the review through a small wrapper around your agent CLI.
    **Codex is bundled and works out of the box**, so the recipes below use
    it. Prefer **Claude**? See [Claude](#claude).

---

## Codex (OpenAI) — GitLab

The fastest path to a first review on a GitLab merge request.

### 1. Prerequisites

Install these once and put them on `PATH` (see
[prerequisites](prerequisites.md) for platform-specific commands):

- **uv**, **Python 3.14+**, **Git**
- **Codex CLI**, authenticated to OpenAI
- **Superpowers + the `code-reviewer` skill** in your Codex config
- **glab** (GitLab CLI) — for cloning/fetching MRs

### 2. Install Bubo

```sh
uv tool install bubo
bubo init           # writes ~/.codex/config.toml (with [profiles.bubo]),
                    # ~/.claude/settings.json, the workspace, and the SQLite DB
```

Want the bleeding edge? Track `main` instead:
`uv tool install git+https://github.com/mountainowl/bubo`.

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

The first run is **dry-run by default** (`[review].dry_run = true`): Bubo
plans findings and writes them to SQLite but posts nothing. Read a
transcript under `~/.local/share/bubo/var/reports/`. When it looks right,
flip `[review].dry_run = false` to start posting inline review threads.

### 5. Schedule it (optional)

`bubo init` rendered ready-to-install cron + systemd units under
`~/.local/share/bubo/deploy/templates/`. See [operate](operate.md) for
the install steps.

---

## Codex (OpenAI) — GitHub

Same as the GitLab recipe, with three changes:

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

## Self-hosted / in-house model (OpenAI-compatible)

The strongest version of Bubo's compliance story: when your org runs its own
OpenAI-compatible gateway — Azure OpenAI, vLLM/TGI, LiteLLM, or an internal
proxy — code, diffs, review state, **and the model call** all stay on your
infrastructure. Nothing leaves.

**The one thing to know:** Bubo never calls the model directly. It shells out to
an agent CLI (Codex by default), so the in-house endpoint is configured in the
**agent's profile**, not Bubo's `env.toml`. A bare `bubo init` ships a
stock-OpenAI profile (`model = "gpt-5.5"`, no provider block), so pointing at an
internal endpoint is one explicit edit — `uv tool install bubo && bubo init`
alone will *not* redirect it.

### 1. Install Bubo (unchanged)

```sh
uv tool install bubo
bubo init      # writes ~/.codex/config.toml [profiles.bubo], the env.toml seed, the DB
bubo doctor
```

### 2. Point the Codex profile at your gateway (the load-bearing step)

Edit `~/.codex/config.toml`. Add a model-provider block and reference it from the
`[profiles.bubo]` profile `bubo init` created:

```toml
[profiles.bubo]
model          = "<internal-model-name>"
model_provider = "inhouse"          # references the block below
sandbox_mode   = "read-only"

[model_providers.inhouse]
name     = "In-house gateway"
base_url = "https://llm.corp.internal/v1"   # your OpenAI-compatible endpoint
env_key  = "OPENAI_API_KEY"                 # env var Codex reads the key from
wire_api = "chat"                           # "chat" or "responses" — match your gateway
```

These are **Codex's** config keys, not Bubo's — confirm the exact field names
against your Codex version.

### 3. Tell Bubo the label + key plumbing

In `$BUBO_ROOT/config/env.toml`:

```toml
[agents]
llm_model       = "<internal-model-name>"   # must match the profile; used for the cost label only
llm_api_key     = "${LLM_API_KEY}"          # Bubo exports this under llm_api_key_env
llm_api_key_env = "OPENAI_API_KEY"          # the env var your gateway/CLI reads
codex_profile   = "bubo"
```

If you track cost, set the `[telemetry]` `*_per_1m` rows to your gateway's rates
(see the [configuration reference](configuration.md#telemetry)).

### 4. Pre-flight the sandbox (the enterprise Linux gotcha)

On hardened Linux hosts — Ubuntu with the AppArmor unprivileged-user-namespace
restriction — Codex's default `read-only` sandbox uses bubblewrap and can fail to
initialize, and a failed sandbox can surface as a misleading clean "no findings"
review. Enterprise hosts are exactly where this bites. Fix it once during
bring-up: see [Troubleshooting](troubleshooting.md).

### 5. Verify before going live

Keep `[review].dry_run = true`, run `bubo-poller` once, and read a transcript
under `var/reports/` to confirm findings come through your in-house model. Then
flip `dry_run = false`.

!!! note "Not OpenAI-compatible?"
    The escape hatch is `[agents].reviewer_command` — override the whole command
    with any CLI that takes the prompt and returns Bubo's JSON findings contract.
    You're locked to neither Codex nor OpenAI.

---

## Claude

Bubo runs the review through a wrapper around your agent CLI, so Claude works
like Codex — only the CLI it calls and the key it uses change.
Install the Claude CLI, then set Claude as your agent in `[agents]`:

```toml
[agents]
llm_model = "<your-claude-model>"
llm_api_key = "<your-anthropic-key>"
llm_api_key_env = "ANTHROPIC_API_KEY"   # the env var the Claude CLI reads
```

Codex ships pre-wired, so reviewing with Claude takes one extra step: point
the wrapper at the Claude CLI for the review. Verify it like Codex — keep
`[review].dry_run = true`, run `bubo-poller` once, and read the transcript
under `var/reports/` to confirm findings come through before you flip
`dry_run` to `false`.
