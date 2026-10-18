# Tasks — F1

Fully functional; implementation complete. Prerequisites: spec.md, plan.md and design contracts.

## Setup
- [x] T001 Inventory every emitter in src/bubo/analytics.py, project_analytics.py, events.py and telemetry/metrics.py; record coverage in docs/pages/telemetry.mdx. FR-001/002/007. Done: each emission path mapped to a capture test.

## US1 — Accurate version attribution
- [x] T002 Add immutable installed-version resolution in src/bubo/analytics.py and test packaged/editable/missing metadata in tests/test_update_version.py. FR-003. Done: version cannot change within a process or expose local paths.
- [x] T003 Wire version into src/bubo/analytics.py and telemetry/metrics.py and any metric producer found by T001; add capture tests in tests/test_analytics.py and tests/test_telemetry_metrics.py. FR-001/002/003/006/007. Done: all emitted shapes and retry fixtures pass SC-001/003.

## US2 — Stable identity
- [x] T004 Define identity snapshot/validation interface beside identity handling in src/bubo/analytics.py; add tests/test_update_identity.py for present, absent, corrupt, replaced and unpersistable identity. FR-004/005/006. Done: no regeneration on upgrade recovery and ordinary first initialization retained.

## Final validation
- [x] T005 Document version and identity semantics in docs/pages/telemetry.mdx; run quickstart.md checks and existing analytics/telemetry regression suite. FR-001–007. Done: SC-001–003 documented with capture evidence; F3 integration remains an explicit downstream gate.

Dependencies: T001 -> T002 -> T003 -> T004 -> T005. No parallel tasks share source files.
US1 is independently testable before US2. All tasks are required for fully functional scope.

T002/T003 acceptance also requires missing-version suppression and the unified consent and legacy opt-out precedence matrix (supersedes independent OTLP consent).
T004 must implement the durable-guard check in every identity initializer and test concurrent first-use,
missing identity under guard, and crash/restart; F3 creates/releases the guard. FR-005/006.

Implementation and fixture validation: see `../003-idle-auto-update/acceptance-2026-10-17.md`. No release or live telemetry validation is implied.
