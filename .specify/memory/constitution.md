<!-- Sync impact: scaffold -> 1.0.0; five user-adopted principles established.
Added delivery constraints, verification workflow, governance. No deferred placeholders.
Existing templates reviewed: spec/plan/tasks support these gates without template changes. -->
# Bubo OSS Constitution

## Core Principles

### I. Preserve OSS Compatibility
Bubo MUST preserve existing OSS behavior outside the approved feature scope.
New configuration MUST have documented defaults and preserve existing configuration validity.
Installation forms explicitly included in a feature MUST have acceptance coverage.

### II. Protect Data and Installation Identity
Changes MUST preserve user data and the persisted installation identifier.
Upgrades, migrations, retries, restarts, and recovery MUST NOT rotate `install_id` or `distinct_id`.
Missing or corrupt identity during upgrade MUST be handled as a recoverable failure,
not silently replaced with a new identity.

### III. Make Updates Recoverable
Updates MUST avoid interrupting active work and exclude concurrent mutation during migration.
Every destructive update step MUST have a tested recovery path preserving the last usable state.
An unsuccessful update MUST NOT silently leave an unusable installation or incompatible database.

### IV. Preserve Telemetry Privacy Controls
Existing analytics opt-outs MUST remain effective, including for update-failure events.
Failure reports MUST exclude credentials and private review content.
Local diagnostics and external telemetry MUST have separate, explicit data contracts.
Automatic-update configuration MUST NOT override analytics privacy controls.

### V. Verify Outcomes and Diagnose Failures
Specifications MUST state observable acceptance checks for success and failure paths.
Update failures MUST produce local, redacted, copy/paste-ready issue reports.
When analytics is enabled, update-failure reporting MUST support delivery after transient outages.
Telemetry MUST identify the installed Bubo version that produced the event or measurement.

## Delivery Constraints
Planning does not authorize implementation, publishing, live telemetry, or deployment.
Fully functional delivery MUST cover all agreed installation types and integrations;
untested assumptions and stubs MUST NOT be represented as completed behavior.

## Verification Workflow
Plans MUST trace requirements to tasks and observable checks. Implementation review MUST verify
compatibility, data and identity preservation, recovery, privacy, and diagnostics.
Failure-injection and end-to-end acceptance evidence are required before declaring updates ready.

## Governance
This constitution governs feature specifications and implementation plans.
Conflicts MUST be resolved before dependent work proceeds. Amendments require an explicit user
decision, a recorded rationale, and rechecking affected artifacts. Semantic versioning applies:
major for incompatible principles, minor for new principles, patch for clarifications.

**Version**: 1.0.0 | **Ratified**: 2026-10-16 | **Last Amended**: 2026-10-16
