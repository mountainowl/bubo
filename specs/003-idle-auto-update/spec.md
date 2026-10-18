# Feature Specification: Idle automatic updates and database recovery

**Feature ID**: F3 / 003-idle-auto-update (planning only)
**Created**: 2026-10-16 | **Mode**: Fully functional | **Status**: Planning ready; implementation not started
**Input**: FEATURES.md R1/R2/R7/R8, integrating R3–R6. Depends on F1 and F2.

## User Scenarios & Testing

### US1 — Stay current without interrupting work (P1)
Default installations discover stable updates, wait until idle and resume service on the new version.
Independent test: use local fake release registry and real temporary installation environments.
1. Given a newer eligible stable release and no active operations, when the scheduled attempt runs,
   then Bubo updates, migrates and returns healthy without a manual command.
2. Given an active CLI/MCP/poll/sync/review operation, when an update is due, then work completes
   normally and replacement waits; new work cannot race with the exclusive migration window.
3. Given auto_update=false, when scheduled/startup checks run, then no new discovery/download/install
   attempt occurs; existing critical recovery may finish to leave a safe state.

### US2 — Survive a failed update without losing work (P1)
A failure automatically restores a compatible runnable release and database with the same identity.
Independent test: kill processes at each transition and run recovery against a temporary installation.
1. Given a migration/health/restart failure, when recovery runs, then the previous release and DB
   are restored together, identity is unchanged, and an F2 report/event records the failure.
2. Given a crash after package activation, when supervisor restarts, then the journal decides recovery
   before accepting new work; stale processes cannot resume against the wrong DB schema.

### US3 — Update across all installation forms safely (P1)
Users keep the installation they chose without lost customizations or unexplained failures.
Independent test: run the installation matrix in contracts/installation.md.
1. Given uv/pipx/writable dedicated pip/clean editable/upstream Docker, when replacement can be made
   reversible, then it follows the same idle, migration and recovery contract.
2. Given shared/system pip permissions, dirty source, derivative Docker image or unsafe storage,
   when replacement cannot be proved safe, then update defers with an actionable local report;
   analytics-enabled genuine blockers also queue an F2 event. Ordinary Bubo operation continues.
3. Given custom Docker launch settings, when replacement occurs, then volumes, identity, environment,
   network/ports, secrets references, user and restart policy are preserved without leaking them.

### US4 — Update manually when automatic updates are disabled (P1)
An operator who sets auto_update=false can explicitly run `bubo update` for the resolved installation.
1. Given automatic updating disabled, when the user runs the command while idle, then the same pinned
   release, migration, validation and recovery workflow runs without enabling future automatic updates.
2. Given active work, when manually requested, then the update defers without interrupting work.
3. Given failure or no newer version, then the command reports the actual result and provides the same
   local failure report when applicable; analytics opt-out remains effective.

## Requirements

- FR-001: Default auto_update=true when omitted; explicit false disables all new automatic checks,
  staging and replacement. Parse a real boolean in existing config; reject malformed values clearly.
- FR-002: Discover newer stable compatible official releases; do not downgrade or select pre-releases.
  Pin the exact version/artifact before staging; verify provenance/integrity and runtime compatibility.
- FR-003: Define idle as zero admitted operations across CLI, MCP, polling, outcome sync and workers
  sharing the affected installation/state. Idle resident processes do not themselves block updates.
- FR-004: Atomically try exclusive admission without waiting; active work immediately defers the update
  without fencing. Fence new operations only after exclusive idle ownership succeeds; never terminate an active review to create idleness. Handle worker handoff races.
- FR-005: Stage and validate replacement before destructive work; hold the exclusive window for
  consistent DB backup, migration, activation, health validation and any necessary recovery.
- FR-006: Migrate automatically, preserve all user data and config, validate integrity and application
  compatibility, and never admit old-generation code against a new-generation DB.
- FR-007: Persist a phase journal before each destructive transition; automatically recover interrupted
  attempts and restore matched code/database/config state. Keep the last known good artifacts until
  success is durable. If automatic recovery itself fails, keep admission closed with actionable diagnostics.
- FR-008: Preserve F1 identity/state-root semantics through every phase, restart and Docker replacement.
- FR-009: Capture F2 diagnostics for all failures and actionable deferrals; external transmission obeys
  analytics consent. Expose a local status/report without requiring access to PostHog.
- FR-010: Support uv tool, pipx, pip, source/editable and Docker with safe deferral when required.
  Use an external Docker controller; never mount an engine socket into the Bubo container implicitly,
  escalate privileges automatically, overwrite local edits, or replace a derivative image with upstream.
- FR-011: A stable helper/supervisor outside the replaced runtime MUST complete or recover the attempt.
  Provide installation lifecycle registration and removal; if no safe controller can be registered,
  report the prerequisite and defer. Existing installations gain this behavior after adopting the
  updater-capable release; this plan does not remotely modify already installed old binaries.
- FR-012: For automatic triggers, re-read auto_update before exclusive admission and activation. If disabled before mutation,
  cancel staging and resume normal work; after mutation begins, finish recovery or commit safely, then
  prevent future attempts. Pending eligible F2 delivery remains governed by analytics consent.
- FR-013: Coalesce duplicate schedules/attempts, bound retries, and avoid unbounded maintenance waits.
  Failed targets must not create a tight retry loop. Report pending/deferred/recovering/current state.
- FR-014: Protect update state/commands against untrusted paths, metadata and concurrent processes;
  do not execute shell text from release metadata. Detect uncoordinated processes/unsafe shared-volume
  locking or multiple state roots before replacement and defer rather than corrupt data.

- FR-015: Provide explicit `bubo update` using the same engine, helper, admission, migration, recovery
  and F2 reporting as automatic updating. Manual invocation overrides auto_update=false only for the
  explicitly targeted installation/state root and this attempt; it never changes saved settings or
  enables future scheduling. Preserve other roots' opt-out safeguards. No force-active-update mode.

## Key Entities

UpdateConfig, ReleaseCandidate, InstallationDescriptor, ActivityLease, AttemptJournal, RecoverySnapshot.
Detailed transitions and installation obligations are in data-model.md and contracts/.

## Success Criteria

SC-001: Each supported safe installation-matrix case completes idle update+migration+restart with
unchanged identity/config and all preexisting logical DB records preserved, without a manual repair.
SC-002: Concurrent operation/worker-handoff tests never migrate during active work or admit stale code.
SC-003: Every injected failure or kill point yields either automatic compatible restoration or an explicit
recoverable blocked state; zero silent corruption or identity rotation. F2 evidence survives restoration.
SC-004: Disabled configuration causes zero automatic update discovery/download/mutation calls in test captures; explicit manual requests follow FR-015.
SC-005: Unsafe installation cases defer with a concrete next action and retain ordinary functionality.
SC-006: With a healthy controller/network and continuous idleness, check within 24 hours and start a
staged eligible update within 60 seconds of the next admission opportunity. Critical maintenance has a
10-minute deadline before cancellation is requested; restoration starts only after mutation writers
are demonstrably quiescent, and recovery is not force-killed to meet that deadline.
SC-007: auto_update=false plus explicit CLI invocation completes a safe update or accurate deferral,
without enabling auto-update; CLI and unattended paths share the same failure/recovery outcomes.
SC-008: a registered unsupervised installation checks/updates while all application CLI processes are
closed, and recovers after helper/process restart. Background setup is required, not silently deferred
merely because no supervisor was present before setup.
All acceptance tests, multi-process tests and lifecycle fault injection are required for release readiness.

## Edge Cases and Assumptions

Single-host local state with OS-backed cross-process locks is the initial executable boundary;
unsafe network filesystems/multi-host deployments are detected and safely deferred, not claimed as tested.
Never infer idleness solely from DB queue rows. Multi-state use of one executable requires registration
and coordinated locks or deferral. No updater can recover simultaneous loss of all storage/backups.
Custom packaging still participates in preflight and diagnostics; automatic unsafe replacement is excluded
by the approved deferral policy. Repeated busy periods may postpone updates indefinitely, without updater-created rejection or delay of busy work.

## Clarifications — 2026-10-16

User selected fully functional, all installation types, external Docker controller plus safe deferral,
no automatic privilege escalation or overwriting local changes, default true config, explicit manual updates when disabled, and honored analytics
opt-outs. Remaining numeric scheduling/retention values are explicit engineering defaults, not user claims.

Aggregate clarification: FR-001/012/014 requires unanimous enabled config for automatic updates across roots sharing code; manual updates use FR-015 and preserve other roots' opt-outs.
FR-007 watchdog cancellation cannot restore until mutation writers are demonstrably quiescent.
FR-006/007 resident restart uses maintenance-generation readiness without normal DB admission;
only the exclusive owner may run migration/health until durable commit and fence release.

Minimum-build review additions: FR-003/004 requires nonblocking idle acquisition with immediate partial-lock
release. FR-009/013 requires silent successful/current/busy checks, deduplicated actionable notices and
unchanged protocol/stdout output. FR-011/014 must preserve help, dry-run and read-only no-write contracts;
unsafe read coordination defers the updater. Existing offline CI and legacy behavior are release gates.

User clarification during three-agent review: retain unattended updating for unsupervised installations
and add CLI-based updates for users who opt out of automatic updating. Keep minimal native background
registration when no existing supervisor is available; lack of permissions may still safely defer, but
absence of a prior supervisor alone is a setup case, not an unsupported installation class.
