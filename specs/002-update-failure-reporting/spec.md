# Feature Specification: Actionable update failures

**Feature ID**: F2 / 002-update-failure-reporting (planning only)
**Created**: 2026-10-16 | **Mode**: Fully functional | **Status**: Planning ready; implementation not started
**Input**: FEATURES.md R3/R4, supporting R2/R5/R6/R7. Depends on F1.

## User Scenarios & Testing

### US1 — Copy an actionable failure report (P1)
An operator can paste a local report into a Bubo issue without collecting internal files.
Independent test: submit synthetic failure contexts to local report generation.
1. Given any failed update phase, when failure is recorded, then a report includes safe phase,
   versions, error code, recovery result and next action, with no credentials or private review content.
2. Given analytics is disabled, when failure occurs, then the same local report remains available.
3. Given a process dies mid-update, when recovery resumes, then the journal yields an interrupted-phase report.

### US2 — Receive failures after connectivity returns (P1)
Maintainers receive sanitized failures without breaking reviews or changing consent.
Independent test: local fake PostHog endpoint accepts, times out, rejects and later recovers.
1. Given enabled analytics and offline transport, when failure occurs, then it persists locally and is retried.
2. Given an ambiguous acknowledgement, when retransmitted, then the same delivery ID is used.
3. Given consent is revoked before send, when retry runs, then nothing is transmitted and pending export is removed.

## Requirements

- FR-001: Every discovery, staging, admission, backup, install, migration, activation, restart, validation,
  recovery and delivery failure MUST leave a local sanitized diagnostic; planned safe deferrals are
  explicitly labeled deferred, not successful. Genuine blockers have reports and enabled-consent events.
- FR-002: Provide a Markdown issue report containing UTC time, attempt/failure IDs, installation type,
  producer/from/target versions (unknown target explicitly allowed), phase, bounded error code,
  sanitized explanation, recovery outcome, and actionable next step. Never auto-file issues.
- FR-003: Persist report and eligible failure event before destructive work/recovery can lose them.
  The diagnostic store MUST survive DB rollback and package/container replacement.
- FR-004: Queue every actual update failure when analytics is enabled and valid identity/version exist;
  retry after transient outages with bounded requests and backoff, without blocking Bubo work.
- FR-005: Honor opt-out both at capture and delivery. Opted-out failures are local only, are not backfilled
  after opt-in, and pending external payloads are deleted upon opt-out; local diagnostics remain.
- FR-006: Preserve original event ID, occurrence time, producer version and identity during retries.
  Delivery is at least once with stable deduplication identity, not a claim of exactly-once processing.
- FR-007: PostHog payload MUST be an allowlist of categories/versions and opaque IDs. No arbitrary
  traceback, exception message, path, repository name, code, token, prompt or review content may be sent.
- FR-008: Queue/store errors and permanent rejection MUST be locally visible. Do not recursively queue
  telemetry-send failures. No unacknowledged event may be silently evicted; stop new update attempts if
  durable recording cannot be guaranteed, preserving normal Bubo work.
- FR-009: Recover crash-interrupted attempts into failure reports from the F3 journal; failure at a
  recovery stage is a distinct failure with the same attempt ID and its own stable failure ID.

## Key Entities

UpdateFailure, IssueReport, DurableDelivery, AttemptJournalReference. See data-model.md.

## Success Criteria

SC-001: Every injected lifecycle failure yields a sanitized local issue report and, when eligible,
a durable event; reports survive rollback. SC-002: Offline/restart/retry tests deliver all eligible failures
once transport recovers, without changing IDs/version/time. SC-003: All opt-out and seeded-secret tests
produce zero prohibited transmissions or secret leaks. SC-004: permanent rejection/storage faults are
visible and never prevent ordinary reviews; new updates safely defer. Tests explicitly required.

## Edge Cases and Assumptions

Read-only/full disk permits no absolute guarantee of a persisted local report: emit sanitized stderr or
supervisor logs and fail closed before new destructive work. Crash recovery uses the durable attempt
journal; simultaneous loss of all persistent storage is outside recoverability guarantees. No immediate
cloud delivery promise while offline or opted out. Old general-purpose analytics remains best effort.

## Clarifications — 2026-10-16

User confirmed existing opt-outs apply. `auto_update=false` stops automatic updates, not consent or delivery of
already eligible failures; delivery still observes current analytics consent. Safe deferrals are approved.
No issue publishing is authorized. No outstanding material clarification.
