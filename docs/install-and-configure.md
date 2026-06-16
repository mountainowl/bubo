# Install and configure

You're a few minutes from a first review. First, check that the runtime and
provider tools in [prerequisites.md](prerequisites.md) are on `PATH` and your
bot/LLM credentials are handy.

## Install

`uv tool install` is the recommended path. The old shell-installer scripts
(`scripts/install-package.sh` and `scripts/deploy-package.sh`) are gone — use
the `uv tool install` flow below (or pipx).

### Option 1 — `uv tool install` (recommended)

One command: an isolated venv managed by uv, with entry points on `PATH`.
Then `bubo init` handles per-host configuration:

```sh
# Install the latest release.
uv tool install bubo

# Place ~/.codex/config.toml, ~/.claude/settings.json, config/env.toml seed,
# the var/ workspace, prompts, skills, plugins, and initialize the SQLite DB.
bubo init

# Verify — non-zero exit on any missing piece. Suitable for cron / monitoring.
bubo doctor
```

Want the latest unreleased code? Track main with
`uv tool install git+https://github.com/mountainowl/bubo`.

`bubo init` supports:

| Flag | Effect |
|---|---|
| `--dry-run` | print every action it would take without touching disk |
| `--force` | overwrite existing `config/env.toml`, `~/.codex/config.toml`, `~/.claude/settings.json` (clobbers operator edits) |
| `--no-agent-config` | skip the `~/.codex/` and `~/.claude/` writes (for hosts that already have a hand-rolled agent config) |
| `--root PATH` | install under `PATH` instead of the default `$BUBO_ROOT` or `~/.local/share/bubo` |

To upgrade, reinstall:

```sh
uv tool install --reinstall bubo
bubo init     # idempotent — re-applies packaged template updates
bubo doctor
```

### Option 2 — pipx (functionally equivalent)

For hosts without uv that already use pipx:

```sh
pipx install bubo
bubo init
```

### Local development

```sh
uv sync --dev
uv run pytest
```

## Configure

`bubo init` seeds the config at `$BUBO_ROOT/config/env.toml` (default
`~/.local/share/bubo/config/env.toml`) from the packaged example. It's **not**
in your current working directory — it lives under the install root and survives
later `bubo init` runs untouched unless you pass `--force`.

Open it and fill in the minimum for a first review:

```sh
"${EDITOR:-vi}" "$BUBO_ROOT/config/env.toml"
```

```toml
[gitlab]
token = "glpat-..."          # api scope

[agents]
llm_model = "gpt-5.5"            # match what your CLI is configured for
llm_api_key = "..."             # your LLM key
llm_api_key_env = "OPENAI_API_KEY"  # the env var your LLM CLI reads it from
                                    # (ANTHROPIC_API_KEY, GEMINI_API_KEY, …)

[[projects]]
path = "your-group/your-repo"
enabled = true
```

Bubo is model-agnostic: `llm_api_key_env` names the variable your LLM CLI/SDK
reads the key from, so you're never locked to one provider. The
[recipes](recipes.md) have end-to-end setups.

Leave `[review].dry_run = true` (the default) until your first review output
looks right — the poller plans findings without posting comments. Every knob
and default lives in the [configuration reference](configuration.md).

## Verify

After editing `env.toml`, confirm the install end to end:

```sh
bubo doctor                # workspace + env.toml + DB + Codex profile
bubo-poller                   # one dry-run poll cycle; exits at the end
```

`doctor` exits non-zero on any missing piece. With `[review].dry_run = true`,
the first `bubo-poller` run records planned findings to SQLite without posting
to the SCM — so you can read `var/reports/*.txt` and judge the agent's output
before flipping `dry_run` to `false`.

## GitLab bot setup

1. Create a bot user, for example `bubo`.
2. Add it to every target GitLab project with permission to read MRs and
   create discussions.
3. Create a token with `api` scope.
4. Put the token in ignored `config/env.toml` under `[gitlab].token`.
5. List the projects under `[[projects]]` in the same file.

## GitHub bot setup

1. Create a bot user (or use a machine account) and add it as a
   collaborator with pull-request read+write on every target repository.
2. Generate a personal access token with pull-request read+write scope.
3. Put the token in ignored `config/env.toml` under `[github].token`.
4. Set `[scm].provider = "github"` (or `BUBO_PROVIDER=github` for a single
   run) and list the projects under `[[projects]]`.

## Where files live after `bubo init`

Handy for reference and operate-time troubleshooting:

| Path | What |
|---|---|
| `$BUBO_ROOT/config/env.toml` | Operator's editable config — your tokens, project list, dry-run toggle |
| `$BUBO_ROOT/var/state/reviewer.sqlite` | Review state, posted findings, outcome metadata |
| `$BUBO_ROOT/var/reports/` | Per-review agent transcripts |
| `$BUBO_ROOT/var/log/` | JSON-line event log (poller, worker, sync) |
| `$BUBO_ROOT/prompts/00-meta.md` | Meta prompt rendered for each review |
| `$BUBO_ROOT/skills/code-reviewer/` | Bundled Codex/Claude code-reviewer skill |
| `$BUBO_ROOT/plugins/superpowers/` | Bundled Superpowers plugin |
| `$BUBO_ROOT/deploy/templates/` | Cron + systemd templates with `{{ROOT}}` already substituted; ready for `sudo install` / `systemctl enable` (see [operate.md](operate.md)) |
| `~/.codex/config.toml` | Codex main config with the load-bearing `[profiles.bubo]` block (skipped under `--no-agent-config`) |
| `~/.claude/settings.json` | Claude settings (skipped under `--no-agent-config`) |
| `~/.codex/skills/code-reviewer` | Symlink to `$BUBO_ROOT/skills/code-reviewer` |
