> Current decision (2026-10-17): only `[analytics].enabled` controls telemetry.
> Legacy environment and `[telemetry].enabled` overrides are ignored. Development
> and tests use disabled configuration and isolated transports. Safe OTel context
> includes model/family, languages, platform, runtime, version and existing identity.

> Updated decision (2026-10-17): the user's follow-up supersedes independent
> PostHog/OTLP consent. One default-on `analytics.enabled` setting controls both;
> OTel uses sanitized PostHog events and legacy opt-outs are preserved. See
> `docs/pages/telemetry.mdx` and the F3 acceptance record for implemented behavior.

# Feature Specification: Version attribution and stable identity

**Feature ID**: F1 / 001-telemetry-version-identity (planning identifier; no branch created)
**Created**: 2026-10-16 | **Mode**: Fully functional | **Status**: Planning ready; implementation not started
**Input**: FEATURES.md R5/R6; also supports R2/R4. Constitution 1.0.0.

## User Scenarios & Testing

### US1 — Attribute every measurement to its Bubo release (P1)
Operators can distinguish behavior before and after an upgrade.
Independent test: capture each metric/export/event channel using test collectors without running updates.
1. Given version A, when any event or metric is emitted, then its producer version is A.
2. Given a queued event produced by A, when B later sends it, then its version remains A.
3. Given B starts after activation, when it emits, then its version is B.

### US2 — Keep installation identity across upgrades (P1)
An existing installation remains the same installation through successful or failed updates.
Independent test: preserve/restore the state root in a simulated upgrade and compare persisted IDs.
1. Given ID X, when update/restart/rollback/container replacement occurs, then install_id and distinct_id remain X.
2. Given a previously present identity becomes missing/corrupt during upgrade, when recovery runs,
   then it fails safely without generating a replacement identity.

## Requirements

- FR-001: Every emitted PostHog event and its property envelope MUST carry nonempty `bubo_version`.
- FR-002: Every emitted metric MUST have producer version in its exported data: `service.version`
  on OpenTelemetry resources and `bubo_version` on standalone metric records; include spans for consistency.
- FR-003: Capture version from the running Bubo distribution before replacement; preserve it on retries.
  Source/editable builds use installed distribution metadata plus a documented source revision qualifier
  when metadata alone cannot distinguish the running build. Never invent the target version on failure.
- FR-004: Preserve the resolved persistent state root and exact valid install ID; distinct_id equals install_id.
  No version, container ID, host change, or rollback may replace these values.
- FR-005: Guard update identity snapshots; missing/corrupt previously established identity blocks activation.
  A legitimately absent identity on a never-initialized or opted-out installation remains absent;
  normal first-use initialization stays distinct from update recovery. Failure to persist suppresses external analytics.
- FR-006: Preserve existing opt-outs and payload allowlists. No high-cardinality installation ID is added
  to metric labels solely for this feature. Version is low-cardinality and may not be overridden by callers.
- FR-007: Cover all current PostHog emitters, project profile emissions, OTLP metrics/spans, and any
  standalone emitted metric records. Historical stored aggregates are not rewritten or falsely version-tagged.

## Key Entities

ProducerVersion (immutable process snapshot); InstallationIdentity (persistent state-root identity);
TelemetryEnvelope (producer version, existing IDs, existing allowed properties).

## Success Criteria

- SC-001: Test captures of every inventoried emitter contain an accurate nonempty producer version.
- SC-002: Byte-for-byte identity preservation through success, restart, rollback and Docker state-volume reuse.
- SC-003: Retry across versions never relabels an older event; zero PostHog events with invalid identity or analytics opt-out.
Tests for these contracts and regressions are explicitly required.

## Edge Cases and Assumptions

Missing metadata, editable source revisions, concurrent emitters, unavailable state filesystem, old queued
records, and identity corruption must be covered. Existing unsent legacy events without source version
must not be mislabeled; preserve as legacy unknown locally and omit external emission if version cannot be recovered.
No retrospective claim can be made about historical code versions that were never recorded.

## Clarifications — 2026-10-16

User selected fully functional and confirmed analytics opt-outs apply. IDs refer to the persisted install ID
and distinct ID, not per-event delivery IDs. Update snapshot protection is consumed by F3.
No remaining material product question for this feature.

Review clarification: version resolution uses distribution metadata or a verified embedded build version;
if neither exists, external version-dependent emission is suppressed with a local diagnostic, while
reviews continue and updates defer. OTLP consent remains independent from PostHog consent.
FR-005 includes a durable upgrade guard honored by every identity initializer, including concurrent
processes and crash restarts. Editable builds disclose dirty/unverified source without leaking content.
