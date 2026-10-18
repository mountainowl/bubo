# Research — F1

Read-only repository evidence, 2026-10-16:
- src/bubo/analytics.py:221–298 persists UUID in state/install_id and sets bubo_version/install_id/distinct_id.
- src/bubo/telemetry/metrics.py:271–316 constructs OpenTelemetry resources without installed version.
- src/bubo/project_analytics.py emits profile data through analytics; verify that route in emitter inventory.
- src/bubo/paths.py separates install files and mutable state; use its resolved path rather than new roots.

Decision: immutable producer version per process, retained in durable payloads. Rationale: metadata can
change under a still-running process; retry-time tagging would misattribute failures. Alternative rejected:
look up version on every send. Resolve before startup hands control to the updater.

Decision: installation identity is state-root scoped, not package/container scoped. Updater snapshots
present/absent state and valid ID, then verifies after migration and recovery. Alternative rejected:
reusing normal missing-ID initialization during recovery, which silently creates a new installation.

Decision: OTel resource service.version plus standalone bubo_version. Rationale: standard resource
metadata travels with every exported metric, without adding identity cardinality. Tests must inspect full
exported payloads, not just local attribute helpers. Unknown installed version blocks a new update
attempt with a local report; ordinary unrelated Bubo work retains existing resilience.
