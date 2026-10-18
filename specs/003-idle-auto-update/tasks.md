# Tasks — F3

Fully functional implementation. F1 and F2 contracts and tests are prerequisites.
Implementation and fixture acceptance are recorded below; this is not a release authorization.
Every install row and required failure path must pass before enabling this feature for release.

## Setup and foundation
- [x] T001 Add auto_update boolean/default parsing through src/bubo/review_config.py and env_config.py, with tests/test_updater.py. FR-001/012. Done: omitted/true/false/invalid and mid-attempt changes covered; false yields no automatic discovery calls.
- [x] T002 Implement independent helper bootstrap, user scheduler registration/removal and installation/state-root inventory in src/bubo/update_helper.py, cli.py and paths.py; test tests/test_update_bootstrap.py. FR-010/011/014. Done: Linux/macOS/Windows ownership, duplicate registration, permissions, uninstall and Docker external-controller setup pass without implicit escalation.
- [x] T003 Implement OS-backed shared/exclusive leases, persistent generation fence and F1 identity guard lifetime in src/bubo/update_admission.py; test tests/test_update_admission.py. FR-003/004/008/014. Done: multi-process race/crash/unknown-root and unsupported-filesystem tests fail closed.
- [x] T004 Integrate leases into src/bubo/cli.py, service.py, poller.py, mcp_server.py and db.py, including overlapping detached-worker handoff. FR-003/004/006. Done: every migrate/read/write path participates, idle residents do not block and stale generations cannot resume.

## US1 — Default idle update
- [x] T005 Implement pinned official stable release discovery/staging/provenance/runtime checks and scheduling in src/bubo/updater.py; test tests/test_updater.py. FR-001/002/005/012/013/014. Done: fake registry/clock verifies no downgrade/prerelease/corrupt artifact or disabled automatic fetch; busy work postpones replacement.

## US2 — Automatic migration and recovery
- [x] T006 Implement versioned intent journal, consistent SQLite backup, config/identity snapshots and compatibility checks in src/bubo/updater.py and db.py; test tests/test_updater.py. FR-005/006/007/008. Done: backup validates, WAL/SHM handling and present/absent identity survive restart, diagnostics excluded from rollback.
- [x] T007 Implement exclusive activate/migrate/health/restart/commit/recovery orchestration in src/bubo/updater.py and update_helper.py. FR-005/006/007/008/012/013. Done: fail/kill injection at every transition restores matched code+DB or stays explicitly fenced; no SCM/review side effects in health probes.
- [x] T008 Wire all phases and recovery failures to F2 src/bubo/update_reporting.py; integrate local status, report, retry-failures and helper fallback in cli.py/update_helper.py. FR-009/011. Done: every phase has a report/eligible event, correct producer/from/target version and unchanged IDs; transport continues when auto-update disabled.

## US3 — Installation matrix
- [x] T009 Implement uv/pipx/pip adapters in src/bubo/update_installers.py and tests/test_update_installers.py. FR-010/014. Done: real temporary environments update and restore full old dependency/runtime/entrypoint state; shared/system/permission hazards defer.
- [x] T010 Implement clean upstream source/editable adapter in src/bubo/update_installers.py and tests/test_update_installers.py. FR-010/014. Done: verified clean source update+restore succeeds; dirty/local divergence/unknown upstream stays untouched with report.
- [x] T011 Implement external Docker controller adapter in src/bubo/update_installers.py and update_helper.py; add container acceptance in tests/test_update_end_to_end.py. FR-010/011/014. Done: both supported image architectures with local fixture images preserve launch settings/state across replacement+rollback; custom/orchestrated/ephemeral/unsafe targets defer.

## Full validation and documentation
- [x] T012 Add exhaustive multi-process, fault-injection and platform/install matrix fixtures in tests/fixtures/updates/ and tests/test_update_end_to_end.py; run this feature's quickstart and F1/F2 regressions. FR-001–015. Done: SC-001–008 and every matrix row have retained evidence; no production traffic.
- [x] T013 Document defaults, setup/adoption, safe deferrals, recovery and opt-out behavior in README.md, docs/pages/auto-update.mdx and docs/pages/telemetry.mdx. FR-001/007/009/010/011/012/013. Done: instructions match actual implemented commands and acceptance evidence, including limitation of older updater-unaware installs.

## Dependencies and execution

F1 -> F2 -> T001 -> T002 -> T003 -> T004 -> T005 -> T006 -> T007 -> T008 -> T009 -> T010 -> T011 -> T012 -> T013.
Adapter implementation can be designed earlier, but cannot activate installs before recovery/reporting gates.
Tasks share modules, so no [P] marker promises unsafe parallel edits. US1/US2 use fixture adapters for
independent validation; production readiness requires US3 and real adapters, not fixtures alone.
Tests exercise negative paths before declaring implementation complete. No task authorizes deployment,
issue creation, merging or live telemetry in the planning phase.

Aggregate acceptance additions: T001/T003/T012 cover conflicting per-root auto_update and concurrent
config toggles. T007/T012 prove all mutation descendants quiescent before restoration, including hung
writers/helper death. T004/T007/T012 prove service/MCP readiness under held exclusive admission
cannot deadlock or start normal DB operations; exclusive-owner health capability remains private.

T002 includes `bubo update configure` registration/explicit consent-policy refresh for the retained
helper, copying only nonsecret analytics switches from authoritative launch descriptors. T013 documents
its privacy purpose and persistent veto; F2 implements enforcement, not broader environment copying.

## Three-agent minimum-build review acceptance

T002 reuses existing init planning/apply and consent-refresh registration code; preserve init idempotence
and --dry-run no-op. T003/T004 use atomic nonblocking idle acquisition, release partial locks immediately,
guard root registration, and leave busy operations undelayed. T011 implements upstream Docker only;
derivative/orchestrated cases get safe deferral, not a plugin or deployment-manager integration.
T008 shares retained-helper control/report handlers with normal CLI; it does not duplicate parsers/stores.
T012 must retain the full existing offline CI suite unchanged and compare baseline results; cover fresh
and populated legacy state, auto_update true/false, absent helper, and telemetry opt-out settings.
Keep tests/test_cli.py dry-run, tests/test_ui_export.py missing DB/hot-WAL read-only, and
 tests/test_mcp_server.py read-only no-init assertions. Add actual-entrypoint checks for JSON/CSV/stdout,
MCP stdio, silent current/busy checks, and one notice per changed actionable blocker. Do not weaken old
assertions. T013 documents setup limitations and read-only consent-refresh semantics.

## Explicit user update-path decision

T002 retains minimal native background registration for unsupervised installations and reuses an existing
supervisor when available; test while CLI processes are closed. Do not remove unattended support.
T005 adds trigger=manual|automatic and routes explicit `bubo update` through the same engine with no
saved-setting change. T008 wires the CLI command using the retained helper, defined exit outcomes and
latest-report default. FR-015. T012 adds US4/SC-007/008: auto_update=false manual success/current/busy/
failure/recovery, analytics opt-out unchanged, saved config unchanged, shared-root opt-out preserved,
and unattended updates after CLI exit on each native platform. T013 documents manual versus automatic
controls and required setup without implying opt-out disables explicit manual updates.

T008/T012 assert actual `bubo update` dispatch does not hold an ordinary shared lease while awaiting
exclusive ownership; otherwise-idle manual update succeeds, concurrent real work still safely defers.
