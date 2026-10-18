# Telemetry Version and Identity Contract

PostHog example: `{"event":"review_finished","properties":{"bubo_version":"A","install_id":"X","distinct_id":"X"}}`.
A/X illustrate versions/IDs, not literal production values. Callers cannot override common identity/version.
OTLP resource: `service.name=bubo`, `service.version=<installed producer version>` on every signal.
Standalone emitted metric record: `bubo_version=<installed producer version>`.

A payload produced by A retains A when B flushes it. Any F2 failure payload also has from_version and
target_version, which do not replace bubo_version. PostHog emission requires a valid persisted identity
and enabled analytics. Source/editable qualifiers must be bounded, exclude paths, and avoid dirty-file content.

## Resolved review findings
Analytics consent gates PostHog only. OTLP uses its existing, independent telemetry enablement setting;
neither channel overrides the other. Test all four combinations of those settings.
Version resolution uses installed distribution metadata, then a verified embedded build version if
available. If neither supplies an actual version, suppress version-dependent external events/metrics,
write a local version-resolution diagnostic, and defer updates; ordinary review functionality continues.
Never use `unknown` as an installed release. A known package version remains usable for editable builds;
qualify with a bounded revision and `dirty` marker when available, and `source-unverified` when revision
cannot be determined. Do not imply an exact clean revision for modified source.

An upgrade guard persisted outside the application DB MUST be consulted by every identity initializer,
not only updater code. It preserves the pre-upgrade present/absent snapshot and survives crashes.
While guarded, initializers must not generate a replacement; absent-before remains absent until recovery
and guard release, after which ordinary first initialization may resume. Concurrent callers and restarts
must obey this guard. F3 owns guard lifetime; F1 implements the shared identity behavior.
