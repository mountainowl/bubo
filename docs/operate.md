# Operate

## Deploy to a host

Recommended single-host install:

```sh
# 1. Install the Python package + entry points into an isolated venv.
uv tool install git+https://github.com/mountainowl/bubo@v0.6.0

# 2. Place the per-host workspace, Codex profile, Claude settings,
#    rendered cron/systemd templates, and SQLite schema.
bubo init

# 3. Verify before scheduling — non-zero exit on any missing piece.
bubo doctor
```

`bubo init` writes under `$BUBO_ROOT` (default
`~/.local/share/bubo`). Pass `--root /opt/bubo` to
override. The init step is **idempotent**: re-running it on an upgrade
is a no-op for `config/env.toml` (operator edits preserved) and a
refresh-overwrite for the packaged prompts/skills/plugins. Use
`--force` to overwrite `config/env.toml` or `~/.codex/config.toml` too.

### Remote host (SSH)

```sh
ssh user@host '
  uv tool install git+https://github.com/mountainowl/bubo@v0.6.0 &&
  bubo init &&
  bubo doctor
'
```

For a fleet, wrap the three lines in your config-management tool of
choice (Ansible, Pyinfra, Salt). The previous bespoke
`scripts/deploy-package.sh` is deprecated; it still works through
v0.6.x with a warning, but will be removed in v0.7.0.

### Upgrades

```sh
uv tool install --reinstall git+https://github.com/mountainowl/bubo@v0.6.1
bubo init                         # idempotent — refreshes packaged assets
bubo doctor                       # confirm
```

### Uninstall

```sh
uv tool uninstall bubo
# Optionally remove the per-host workspace (SQLite state lives here):
rm -rf "${BUBO_ROOT:-$HOME/.local/share/bubo}"
# Optionally remove the agent configs the installer wrote:
rm -f ~/.codex/config.toml ~/.claude/settings.json
rm -f ~/.codex/skills/code-reviewer            # this is a symlink
```

## Schedule the poller

> **`bubo init` does NOT install cron entries or systemd units.**
> Scheduling is a separate, deliberate step — operators run bubo
> under different regimes (cron, systemd, Kubernetes CronJob, Nomad, …)
> and the install path stays scheduler-agnostic.

`bubo init` materializes three ready-to-install templates under
`$BUBO_ROOT/deploy/templates/`. The `{{ROOT}}` placeholder
has already been substituted with your actual install path, so the
files are ready for `sudo install` / `systemctl enable` with no
further hand-editing required. Pick **one** of the two paths below;
both achieve the same cadence (poll every 15 min, sync outcomes
hourly, health probe every 5 min) and both ship with **separate
`flock` files per role** (poller / outcome-sync / health) to prevent
the cross-role lock collision that broke a production deploy in 0.5.0
(see [CHANGELOG.md](https://github.com/mountainowl/bubo/blob/main/CHANGELOG.md) `[0.5.1]`).

### Cron

```sh
# Create the lock dir owned by the runtime user (once).
sudo install -d -o bubo -g bubo -m 0755 /var/run/bubo

# Install the cron drop-in. The exact directory is distro-specific:
#   Debian / Ubuntu / RHEL → /etc/cron.d/
#   macOS launchd hosts    → use the systemd path or convert manually
sudo install -m 0644 \
  "$BUBO_ROOT/deploy/templates/bubo.cron" \
  /etc/cron.d/bubo
```

The three lines fire `bubo-poller` (poll cycle),
`bubo-poller --sync-outcomes` (outcome grading), and
`bubo-poller --health` (liveness probe) at staggered cadences.
Each invocation is a single exit — there is no daemon mode, so tight
intervals are safe.

### systemd

```sh
sudo cp "$BUBO_ROOT/deploy/templates/bubo.service" /etc/systemd/system/
sudo cp "$BUBO_ROOT/deploy/templates/bubo.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bubo.timer
```

The service file uses `LoadCredential=` to inject secrets from
`/etc/bubo/credentials/`, so tokens stay off-disk in
`config/env.toml`. Pair with TOML env interpolation in the config:

```toml
[gitlab]
token = "${GITLAB_TOKEN}"

[agents]
llm_api_key = "${LLM_API_KEY}"
```

## Verify

`bubo doctor` runs four non-mutating checks:

| Check | What fails it |
|---|---|
| Workspace dirs (`config/`, `var/state`, `var/work`, `var/log`) | Init never ran, or the install root moved without re-init. |
| `config/env.toml` present | Operator hasn't seeded the config yet. |
| SQLite DB initialized | Schema-init step skipped or DB file deleted. |
| `~/.codex/config.toml` contains `[profiles.bubo]` | Codex profile missing or hand-edited away — the v0.5.0 incident's exact failure mode. |

The Codex profile check is suppressed by `--no-agent-config` for hosts
that hand-roll the Codex config. Doctor returns 0 on full pass and
non-zero on any failure, so it slots cleanly into a cron-driven
liveness probe.

## Outcome sync

`--sync-outcomes` grades posted findings against current SCM state. It
runs from the same cron line `bubo init` materializes; you
don't need to invoke it by hand once scheduling is set up.

```sh
bubo-poller --sync-outcomes
```

Records whether each finding was resolved, left unresolved after merge,
deleted, replied to, marked disputed, marked false-positive, or marked
duplicate — feeding the `llm_review.findings{status=…}` counter
described in [telemetry.md](telemetry.md).

## Backfill — one-shot, not a cron job

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
bubo-poller --backfill-gitlab-bot-comments-since 2026-05-25T00:00:00Z

# GitHub
bubo-gh-poller --backfill-github-bot-comments-since 2026-05-25T00:00:00Z

# Then grade the imported rows once:
bubo-poller --sync-outcomes
```

Both backfill commands are idempotent — a comment already in SQLite is
upserted, not duplicated — so re-running with a different cutoff is
safe.

## Migrating from the shell installer

Existing operators who deployed with `scripts/install-package.sh` /
`scripts/deploy-package.sh` (v0.5.x and earlier) can stay on that path
through v0.6.x — it prints a deprecation warning but still works.
Migrate at your convenience before v0.7.0:

```sh
# On the host that has a working v0.5.x install:
uv tool install git+https://github.com/mountainowl/bubo@v0.6.0
bubo init                                # idempotent — keeps env.toml
bubo doctor                              # confirm

# Optional cleanup once you're satisfied with the new install:
rm -rf "$BUBO_ROOT/bin" \
       "$BUBO_ROOT/scripts" \
       "$BUBO_ROOT/src"               # left over from shell-installer copy
```

State (`var/state/reviewer.sqlite`) and operator config
(`config/env.toml`) are preserved across the migration — the new
`bubo init` shares the same workspace layout as the old shell
installer.
