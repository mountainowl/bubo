# Operate

## Deploy to a host

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

## Schedule the poller

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

## Outcome sync

`--sync-outcomes` grades posted findings against current SCM state. It
runs from the same cron line the install template provides; you don't
need to invoke it by hand once scheduling is set up.

```sh
bin/mr-review-poller --sync-outcomes
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
bin/mr-review-poller --backfill-gitlab-bot-comments-since 2026-05-25T00:00:00Z

# GitHub
bin/gh-review-poller --backfill-github-bot-comments-since 2026-05-25T00:00:00Z

# Then grade the imported rows once:
bin/mr-review-poller --sync-outcomes
```

Both backfill commands are idempotent — a comment already in SQLite is
upserted, not duplicated — so re-running with a different cutoff is
safe.
