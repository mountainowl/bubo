# Research — F3

## Evidence
- pyproject.toml:6,17–20: Python >=3.14 and bubo/bubo-poller/bubo-mcp entrypoints.
- service.py:379–456: singleton service lock plus polling/sync scheduling, not global activity lock.
- poller.py:733: detached worker handoff; mcp_server.py:183–290: tools invoke DB migration too.
- db.py:86–103: additive migrations; db.py:657 queue count does not prove global inactivity.
- Dockerfile:35–46: installed as root, runtime UID 10001 cannot replace itself or its container.
- Dockerfile:7–10 permits custom agent derivative images; preserve them by safe deferral.
- paths.py separates mutable state; .github/workflows/publish-docker.yml publishes GHCR version tags
  and latest for Linux amd64/arm64. Current CI is Ubuntu/Python 3.14, not broad platform validation.

## Decisions and alternatives

Use shared operation leases plus nonblocking exclusive idle acquisition, not queue-length inference. Lease spans
CLI work, MCP request, poll/sync cycle and child handoff. Persist generation fencing and upgrade guard;
OS locks alone release on process death and cannot remember unfinished migration. Do not add a
network lock service: shared network/multi-host state defers pending a separately agreed deployment.

Use SQLite backup API under exclusion, with integrity check and schema/release compatibility marker.
Migrate on the new runtime before admitting work. Restore DB including WAL/SHM handling under exclusive
ownership and restore code as a matched pair. Do not copy a live SQLite main file or rely on reverse SQL.
All migrations execute behind admission, including legacy init/migrate calls on MCP/CLI entrypoints.

Retain independent helper, journal and last good runtime outside the mutable target environment.
It uses stable stdlib/runtime dependencies and survives package manager failure; self-upgrade is staged
with backward-compatible journal reader before retiring its predecessor. Reject running the updater
only inside the environment it replaces. Old, updater-unaware processes cannot honor leases: detect
and defer until normal restart; never infer they are safely idle from missing new-protocol leases.

Official PyPI bubo artifacts and official GHCR image references are selected over arbitrary update URLs.
Require TLS registry identity and pinned SHA-256/image digest from official release metadata, reject
unexpected redirects/source/package identity, test corrupt artifacts. Do not invent a signing key or
claim signatures exist. Source installs use clean known upstream release tags/commits and fetch
without overwriting local branches; if authenticity or rollback safety cannot be established, defer.

Native helper scheduling uses user-level systemd timer (Linux), LaunchAgent (macOS), Task Scheduler
(Windows); Docker uses an external user-authorized host controller with existing engine permissions.
Missing scheduling facilities/permissions defer updates with setup guidance. Installer/init integration
registers exactly one helper per resolved installation and removes only its own registration on uninstall.
No runtime sudo, container socket mounting, arbitrary controller discovery or credential duplication.

Defaults: daily release checks, <=15-minute startup jitter, admission revisit <=60 seconds, no pre-maintenance drain wait, 10-minute critical-phase watchdog
requesting cancellation; restoration waits for verified writer quiescence. Failed target retry at most once per 24 hours unless explicit local retry; no auto
retry during unresolved recovery. Normal workload is never killed to meet an update deadline.
