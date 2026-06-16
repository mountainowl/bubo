# Operate

This page covers running Bubo in production: deploying to a host, scheduling
the poller, grading outcomes, governance reporting, and one-shot backfill.

## Deploy to a host

Single-host install, three commands:

```sh
# 1. Install the package + entry points into an isolated venv.
uv tool install bubo

# 2. Lay down the per-host workspace, Codex profile, Claude settings,
#    rendered cron/systemd templates, and SQLite schema.
bubo init

# 3. Verify before scheduling — non-zero exit on any missing piece.
bubo doctor
```

`bubo init` writes under `$BUBO_ROOT` (default `~/.local/share/bubo`); pass
`--root /opt/bubo` to override. Init is **idempotent**: on an upgrade it leaves
`config/env.toml` alone (your edits are preserved) and refresh-overwrites the
packaged prompts/skills/plugins. Add `--force` to overwrite `config/env.toml`
or `~/.codex/config.toml` too.

### Remote host (SSH)

```sh
ssh user@host '
  uv tool install bubo &&
  bubo init &&
  bubo doctor
'
```

For a fleet, wrap the three lines in your config-management tool (Ansible,
Pyinfra, Salt). The old bespoke `scripts/deploy-package.sh` is gone — use the
`uv tool install` flow above.

### Upgrades

```sh
uv tool install --reinstall bubo
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
> Scheduling is a separate, deliberate step — operators run Bubo under
> different regimes (cron, systemd, Kubernetes CronJob, Nomad, …), so the
> install path stays scheduler-agnostic.

`bubo init` materializes three ready-to-install templates under
`$BUBO_ROOT/deploy/templates/`. The `{{ROOT}}` placeholder is already
substituted with your install path, so the files are ready for `sudo install` /
`systemctl enable` with no hand-editing. Pick **one** of the two paths below.
Both run the same cadence (poll every 15 min, sync outcomes hourly, health
probe every 5 min) and both ship **separate `flock` files per role** (poller /
outcome-sync / health) to prevent the cross-role lock collision that broke a
production deploy in 0.5.0 (see
[CHANGELOG.md](https://github.com/mountainowl/bubo/blob/main/CHANGELOG.md)
`[0.5.1]`).

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
`bubo-poller --sync-outcomes` (outcome grading), and `bubo-poller --health`
(liveness probe) at staggered cadences. Each invocation runs once and exits —
there's no daemon mode, so tight intervals are safe.

### systemd

```sh
sudo cp "$BUBO_ROOT/deploy/templates/bubo.service" /etc/systemd/system/
sudo cp "$BUBO_ROOT/deploy/templates/bubo.timer"   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bubo.timer
```

The service file uses `LoadCredential=` to inject secrets from
`/etc/bubo/credentials/`, keeping tokens out of `config/env.toml` on disk.
Pair it with TOML env interpolation in the config:

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

Pass `--no-agent-config` to skip the Codex profile check on hosts that
hand-roll the Codex config. Doctor returns 0 on a full pass and non-zero on any
failure, so it drops cleanly into a cron-driven liveness probe.

## Outcome sync

`--sync-outcomes` grades posted findings against current SCM state. It runs
from the cron line `bubo init` materializes, so once scheduling is set up you
never call it by hand.

```sh
bubo-poller --sync-outcomes
```

It records whether each finding was resolved, left unresolved after merge,
deleted, replied to, marked disputed, marked false-positive, or marked
duplicate — feeding the `llm_review.findings{status=…}` counter described in
[telemetry.md](telemetry.md).

### Reply classification

A thread can be resolved because the developer **fixed** the finding or because
they **rejected** it ("working as intended", "not a blocker"). The `resolved`
flag alone can't tell them apart, which would overcount the reviewer's
precision. So when a finding's discussion has a developer reply and no explicit
dispute marker, `--sync-outcomes` asks an LLM to read the bot's finding plus the
reply and decide whether it was **accepted** or **rejected**. A rejection sets
`disputed` (and `false_positive` when the reply says the finding is factually
wrong).

- **Model-agnostic.** Classification runs the same agent you set in
  `[agents].reviewer_command` (default `codex exec --profile bubo`; a custom
  command like `claude -p` is reused as-is). The review contract lives in the
  prompt, not the command, so the same command makes the accept/reject call here.
- **Classified once.** Each finding is classified one time (tracked by the
  `reply_classified` column) to bound LLM cost across the hourly sync. A reply
  that arrives *after* the first classification isn't re-graded.
- **Cold-start safe.** At most a few dozen findings are classified per run, so
  the first sync after upgrading — when the whole backlog of resolved-with-reply
  findings is unclassified — won't fire hundreds of agent calls at once. The
  backlog drains over later runs.
- **Fail-safe.** A *transient* classifier failure (timeout, non-zero exit)
  leaves the finding unclassified and retries on a later sync; unparseable
  output degrades to "unclear". Either way the sync keeps going.

Prefer the explicit path? Tag a reply with `[llm-review:disputed]` /
`[llm-review:false-positive]` — an explicit marker short-circuits the LLM call.

## Governance report

`bubo report` assembles an auditable governance report from the metrics already
in SQLite: review counts (with a **no-findings acknowledgements** rollup), a
**provenance breakdown** (counts by band/source), the **accept-vs-dispute
rate**, a **noise trend** (daily false-positives), a **bug-catch ROI proxy**,
**review latency** (p50/p95/max/avg seconds), **per-category dispute rates**,
token/cost rollups, **policy-decision stats** (from the Phase 2
`governance_decisions`), and a per-change **audit trail**. It's the
compliance-facing companion to the provenance + governance-decision capture in
[configuration.md](configuration.md), "Governance & provenance".

It is strictly **READ-ONLY** — it only queries existing state, never mutates it,
so it's safe to run from a monitoring cron alongside `bubo doctor`. It's also
**self-hosted**: the data comes from your own SQLite and never leaves your
infrastructure, which is the whole compliance pitch for regulated and enterprise
governance teams.

```sh
# Full nested report as JSON (the default).
bubo report

# A week of the per-change audit trail as CSV.
bubo report --since-hours 168 --format csv > audit.csv
```

### Flags

| Flag | Default | What it does |
|---|---|---|
| `--format {json,csv}` | `json` | Output format. JSON is the full nested report; CSV is a single tabular section. |
| `--section` | `audit` | CSV only — which section to emit. `audit` (the per-change audit trail), `noise_trend`, or `dispute_classes`. Ignored for JSON. |
| `--since-hours` | `24` | Rolling window, in hours, for the report. |
| `--since` / `--until` | unset | ISO dates bounding a fixed audit window. Override `--since-hours` for a reproducible window. |
| `--project` | all | Restrict the report to a single project path. |
| `--limit` | unset | Cap the number of audit-trail rows. |
| `--root` | `$BUBO_ROOT` | Workspace root to read state from (same meaning as elsewhere). |

### JSON vs CSV

- **JSON** (default) is the full nested report. Sections: `meta`, `reviews`
  (with a nested `acknowledgements` rollup), `provenance`, `outcomes`,
  `noise_trend`, `roi`, `latency`, `dispute_classes`, `policy_decisions`, and
  `audit`. Scalar rollups (review counts, accept/dispute rate, ROI proxy,
  latency, token/cost) are **JSON-only** — they have no tabular form.
- **CSV** is a single tabular section, picked with `--section`. The default is
  the per-change `audit` trail; `--section noise_trend` emits the daily
  false-positive trend, and `--section dispute_classes` the per-category dispute
  rates (CLI path: raw stats, no `would_suppress`). Use CSV for spreadsheet/BI
  import of the row-shaped sections.

### MCP tool

The same report is available to MCP-capable chat clients (Codex, Claude Desktop,
Cline) as `get_governance_report(since_hours, since, until, project)`. It
returns the same nested JSON as `bubo report --format json`, so a governance
analyst can pull the report into a chat session without shell access.

A companion `get_dispute_classes(project)` tool returns just the per-category
dispute stats (cumulative — they span all recorded outcomes, not a rolling
window, so the tool takes no window argument). Unlike the CLI path, it reads
your real `[review].dispute_suppress_threshold` / `dispute_suppress_min_samples`
and adds a truthful `would_suppress` flag per category — so an analyst can see
which classes the suppression filter *would* drop if enabled, computed against
your actual thresholds rather than a hardcoded guess.

Policy-decision stats are populated only when Phase 2 governance is enabled
(`[governance].policy_mode` not `off`). When it's off, the `policy_decisions`
section reports `available: false` rather than empty counts, so an auditor can
tell "no decisions recorded" apart from "governance decisions were never turned
on".

## Backfill — one-shot, not a cron job

The backfill commands import bot comments that **already exist on the SCM** into
local SQLite. Reach for them when:

- You just deployed against a project where the bot has posted before from
  another install.
- You reset `var/state/reviewer.sqlite` (test, rebuild, host migration) and need
  the per-finding metrics to reflect history, not just go-forward.

They are **deliberately off the cron schedule.** Each run scans every MR/PR
updated since the cutoff — fine for a one-shot recovery, wasteful every 15
minutes. Run them once, then let `--sync-outcomes` take over for go-forward
grading.

```sh
# GitLab
bubo-poller --backfill-gitlab-bot-comments-since 2026-05-25T00:00:00Z

# GitHub
BUBO_PROVIDER=github bubo-poller --backfill-github-bot-comments-since 2026-05-25T00:00:00Z

# Then grade the imported rows once:
bubo-poller --sync-outcomes
```

Both backfill commands are idempotent — a comment already in SQLite is upserted,
not duplicated — so re-running with a different cutoff is safe.

## Migrating from the shell installer

The `scripts/install-package.sh` / `scripts/deploy-package.sh` shell installers
(v0.5.x and earlier) have been **removed**. If you're still running an old
shell-installer deployment, migrate to the `uv tool install` path:

```sh
# On the host that has a working shell-installer deploy:
uv tool install bubo
bubo init                                # idempotent — keeps env.toml
bubo doctor                              # confirm

# Optional cleanup once the new install looks good:
rm -rf "$BUBO_ROOT/bin" \
       "$BUBO_ROOT/scripts" \
       "$BUBO_ROOT/src"               # left over from shell-installer copy
```

State (`var/state/reviewer.sqlite`) and operator config (`config/env.toml`)
survive the migration — `bubo init` shares the same workspace layout as the old
shell installer.
