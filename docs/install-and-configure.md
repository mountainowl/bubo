# Install and configure

Before you start, make sure the runtime and provider tools listed in
[prerequisites.md](prerequisites.md) are on `PATH` and the bot/LLM
credentials are in hand.

## Install

The recommended install path is `uv tool install` against the GitHub
release. The previous shell-installer scripts (`scripts/install-package.sh`
and `scripts/deploy-package.sh`) still work for one minor version with a
deprecation warning, but they will be removed in v0.7.0.

### Option 1 — `uv tool install` (recommended)

Single command, isolated venv managed by uv, entry points placed on
`PATH`. Then `llm-reviewer init` handles per-host configuration:

```sh
# Install the latest tagged release.
uv tool install git+https://github.com/mountainowl/ai-code-review@v0.6.0

# Place ~/.codex/config.toml, ~/.claude/settings.json, config/env.toml seed,
# the var/ workspace, prompts, skills, plugins, and initialize the SQLite DB.
llm-reviewer init

# Verify — non-zero exit on any missing piece. Suitable for cron / monitoring.
llm-reviewer doctor
```

`llm-reviewer init` supports:

| Flag | Effect |
|---|---|
| `--dry-run` | print every action it would take without touching disk |
| `--force` | overwrite existing `config/env.toml`, `~/.codex/config.toml`, `~/.claude/settings.json` (clobbers operator edits) |
| `--no-agent-config` | skip the `~/.codex/` and `~/.claude/` writes (for hosts that already have a hand-rolled agent config) |
| `--root PATH` | install under `PATH` instead of the default `$LLM_CODE_REVIEW_ROOT` or `~/.local/share/llm-reviewer` |

To upgrade, re-run `uv tool install` against the new tag:

```sh
uv tool install --reinstall git+https://github.com/mountainowl/ai-code-review@v0.6.1
llm-reviewer init     # idempotent — re-applies packaged template updates
llm-reviewer doctor
```

### Option 2 — pipx (functionally equivalent)

For hosts that don't have uv but already use pipx:

```sh
pipx install git+https://github.com/mountainowl/ai-code-review@v0.6.0
llm-reviewer init
```

### Option 3 (deprecated) — shell installer

Still works in v0.6.x but prints a deprecation warning on every run. Will
be removed in v0.7.0:

```sh
git clone https://github.com/mountainowl/ai-code-review.git
cd ai-code-review
git checkout v0.6.0
./scripts/install-package.sh --install-agent-config
```

### Local development

```sh
uv sync --dev
uv run pytest
```

## Configure

`llm-reviewer init` seeds the config at
`$LLM_CODE_REVIEW_ROOT/config/env.toml` (default
`~/.local/share/llm-reviewer/config/env.toml`) from the packaged
example. The file is **not** under your current working directory — it
lives under the install root and stays untouched on subsequent
`llm-reviewer init` runs unless you pass `--force`.

Open it and fill in the minimum to get a first review running:

```sh
"${EDITOR:-vi}" "$LLM_CODE_REVIEW_ROOT/config/env.toml"
```

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

Keep `[review].dry_run = true` (the default) until your first real
review output looks right — the poller will plan findings without
posting comments. The full set of knobs and defaults lives in the
[configuration reference](configuration.md).

## Verify

After editing `env.toml`, confirm the install end-to-end:

```sh
llm-reviewer doctor                # workspace + env.toml + DB + Codex profile
mr-review-poller                   # one dry-run poll cycle; exits at the end
```

`doctor` exits non-zero on any missing piece. The first
`mr-review-poller` run with `[review].dry_run = true` records planned
findings to SQLite without posting comments to the SCM, so you can
read `var/reports/*.txt` and decide whether the agent's output looks
right before flipping `dry_run` to `false`.

## GitLab bot setup

1. Create a bot user, for example `llm-reviewer`.
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
4. Set `[scm].provider = "github"` (or run `gh-review-poller`, which
   forces it) and list the projects under `[[projects]]`.

## Where files live after `llm-reviewer init`

For reference and operate-time troubleshooting:

| Path | What |
|---|---|
| `$LLM_CODE_REVIEW_ROOT/config/env.toml` | Operator's editable config — your tokens, project list, dry-run toggle |
| `$LLM_CODE_REVIEW_ROOT/var/state/reviewer.sqlite` | Review state, posted findings, outcome metadata |
| `$LLM_CODE_REVIEW_ROOT/var/reports/` | Per-review agent transcripts |
| `$LLM_CODE_REVIEW_ROOT/var/log/` | JSON-line event log (poller, worker, sync) |
| `$LLM_CODE_REVIEW_ROOT/prompts/00-meta.md` | Meta prompt rendered for each review |
| `$LLM_CODE_REVIEW_ROOT/skills/code-reviewer/` | Bundled Codex/Claude code-reviewer skill |
| `$LLM_CODE_REVIEW_ROOT/plugins/superpowers/` | Bundled Superpowers plugin |
| `$LLM_CODE_REVIEW_ROOT/deploy/templates/` | Cron + systemd templates with `{{ROOT}}` already substituted; ready for `sudo install` / `systemctl enable` (see [operate.md](operate.md)) |
| `~/.codex/config.toml` | Codex main config with the load-bearing `[profiles.llm-reviewer]` block (skipped under `--no-agent-config`) |
| `~/.claude/settings.json` | Claude settings (skipped under `--no-agent-config`) |
| `~/.codex/skills/code-reviewer` | Symlink to `$LLM_CODE_REVIEW_ROOT/skills/code-reviewer` |
