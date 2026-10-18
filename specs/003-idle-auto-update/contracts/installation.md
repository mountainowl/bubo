# Installation Acceptance Matrix

All rows require idle admission, backup, integrity/provenance, migration, identity preservation, F2
failure reporting, restart and matched code/DB recovery. A detected unsafe condition is a reported
deferral, never a silent unsupported-install skip or a successful update.

| Type | Safe update mechanism | Preflight / deferral | Required acceptance |
|---|---|---|---|
| uv tool | owning uv manager, pinned target; retain old environment/dependencies and launcher mapping for restoration | manager absent, environment shared/unregistered, permissions or reversible switch unavailable | real temp uv install update+rollback, manager interruption |
| pipx | owning pipx manager, pinned target, recover prior environment and entrypoints | manager absent or unsafe environment restoration | temp pipx success+failure+restart |
| pip | dedicated writable environment; retain exact old environment/dependencies, install pinned artifact | shared/system environment, permissions, missing dependency rollback material | venv success+rollback; system/shared permission deferral |
| source/editable | clean verified upstream checkout, explicit recorded revision and recoverable dependency environment; update to verified release without overwriting branch changes | dirty/local commits/unknown upstream/shared environment | clean checkout success+restore; dirty/local divergence unchanged+report |
| upstream Docker | external host controller stages exact official image digest, retains old container/image and persistent state, restores same launch descriptor | missing engine permission/controller; unknown owner/orchestrator; unsafe volume locking | amd64/arm64 image swap, crash/restart/rollback, persistent ID/config and launch semantics |
| derivative Docker | preflight and reported safe deferral only in this build; no custom-image update extension | never replace a derivative with upstream; retain bundled agents and report the prerequisite | custom bundled agent unchanged, clear deferral, no upstream swap |

Docker controller owns only explicitly registered containers. Preserve bind/named volumes, environment
and secret references, networking/ports, user/security settings, labels and restart policy locally.
Compose/orchestrated deployments defer in this build; no deployment-manager integration is introduced;
do not recreate individual containers behind that owner's back. `--rm`/ephemeral one-shot invocations
have no safe resident replacement target and defer with controller setup guidance. State and diagnostics
must be on persistent storage before any replacement. Host controller permissions are granted through
normal installation setup, never implicit engine-socket exposure inside Bubo.

Native registration and uninstall tests cover systemd/LaunchAgent/Task Scheduler. Unsupported platforms
or scheduling unavailable: defer with concrete prerequisites. Auto-update false leaves F2 helper delivery
alive where registered. Multi-host/shared state or shared code with unknown state roots defers safely.
