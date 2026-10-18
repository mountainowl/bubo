# Feature-Set Analysis and Handoff

2026-10-16. Fully functional scope. **Planning ready; implementation has not started.**
Constitution 1.0.0. Canonical feature/requirement list: [FEATURES.md](FEATURES.md).
No unresolved blocking findings or user decisions remain in this planning pass.

## Spec Kit workflow actually performed

Read installed Specify, Clarify, Plan, Tasks, Analyze and Constitution stage instructions.
Initialized Spec Kit only after explicit user authorization. Established the constitution from the
user-adopted principles, including clarified telemetry opt-outs. Created feature directories sequentially
with create-new-feature.sh; ran setup-plan.sh and setup-tasks.sh for each. These installed scripts
write feature state without creating Git branches. No extension hooks were configured.
Clarifications were collected from the user before dependent scope/privacy/installation planning.
Manual source-grounded consistency analysis used the installed Analyze rubric, not implementation tests.
check-prerequisites.sh --require-tasks --include-tasks succeeded for all three feature directories.

## Requirement coverage

| Source | Feature requirements | Implementation tasks | Observable acceptance |
|---|---|---|---|
| R1 default idle update | F3 FR-001–005/011–014 | F3 T001–005/012 | No active-operation replacement; default/disable/scheduling captures |
| R2 seamless migration/recovery | F3 FR-005–008 | F3 T006/007/012 | Compatible code+DB restored after every kill point |
| R3 copy/paste failures | F2 FR-001–003/008/009; F3 FR-009 | F2 T001–003/005/006; F3 T008 | Sanitized report usable even if Bubo package is broken |
| R4 PostHog failures + opt-out | F2 FR-004–009; F3 FR-009/011 | F2 T003–006; F3 T002/008 | Durable immutable retries, authoritative consent, no opt-out sends |
| R5 installed version everywhere | F1 FR-001–003/006/007 | F1 T001–003/005; F2 T004; F3 T008 | Every emitter capture attributed to producer version |
| R6 stable installation/distinct ID | F1 FR-004/005; F3 FR-008 | F1 T004/005; F3 T003/006/007/012 | Shared initializer guard, exact identity across recovery |
| R7 default-true config + false disable | F3 FR-001/012/013 | F3 T001/003/005/012/013 | No automatic discovery when disabled; shared-root unanimity; safe in-flight recovery |
| R9 manual update with automatic opt-out; unattended support | F3 FR-011/015 | F3 T002/005/008/012/013 | Manual CLI with false keeps settings; native helper updates after CLI exit |
| R8 all install types + safe deferral | F3 FR-010/011/014 | F3 T002/009–013 | Every installation matrix row has update/recovery or safe-deferral evidence |

31 feature-local functional requirements map to 24 ordered tasks: F1 7 requirements/5 tasks;
F2 9 requirements/6 tasks; F3 15 requirements/13 tasks. Every task is future work and remains unchecked.
Dependency graph is F1 -> F2 -> F3, with no cycle. F1/F2 fixture tests do not substitute for F3's
real installation and end-to-end acceptance gates. No speculative infrastructure feature was added.

## Per-feature and aggregate independent review

Two separate read-only agents performed requirements review and adversarial review using actual files.

| Review | Findings adjudicated and corrected | Final outcome |
|---|---|---|
| F1 | Unknown version suppression; independent OTLP vs PostHog controls; persistent guard for all identity initializers; canonical existing telemetry docs; dirty-source qualifiers | Both reviewers accepted substantive fixes; minor wording reconciled |
| F2 | Operator retry/status interface; usable independent report command; versioned diagnostic compatibility across rollback; delivery owner when auto_update=false | Both reviewers confirmed fixes |
| F3 + aggregate cycle 1 | Shared-root config conflicts; late mutation writers racing restoration; helper environment bypassing app opt-out; resident readiness deadlock | Explicit contracts and task/acceptance obligations added |
| Aggregate cycle 2 | Rechecked revised contracts and cross-feature flow | Both reviewers reported no unresolved blocking contradictions |

Final nonblocking wording cleanup changed the ten-minute watchdog from “start recovery” to “request
cancellation”; restoration is permitted only after writer quiescence. This matches the reviewed contract.
Lead reviewed coverage, terminology, consistency, task dependencies and constitution compliance.
No unresolved ambiguity, duplication, underspecification or constitution blocker identified.
These outcomes are agent document-review judgments, not measured runtime safety or performance.

## Shared contracts and end-to-end journey

F3 defaults to enabled; establishes supported helper/consent registration and installation inventory;
stages a pinned release, atomically acquires exclusive idle ownership without waiting, then fences
admissions and snapshots identity and DB,
mutates/migrates under exclusive ownership, validates and restarts residents into a new generation,
then commits before reopening. Every failure invokes F2, whose store/helper survive DB and package
rollback. F1 ensures original producer version and persistent identity survive replay. Opt-out vetoes
PostHog, independently of auto_update and OTel configuration. Unsafe installation cases defer as approved.

## Checks actually run

- Spec Kit prerequisite validation for each feature including tasks and design documents: PASS.
- Generated Markdown links, placeholder and trailing-whitespace inspection: PASS at final verification.
- git diff --check: PASS. Generated untracked documents additionally checked directly.
- Independent per-feature reviews and two aggregate review cycles: completed as recorded above.
- Application tests, container updates, migrations, network transport and failure injection: NOT RUN;
  no implementation exists for these planned features, and this request authorizes planning only.

## Nonblocking limitations and implementation gates

Existing CI evidence is Ubuntu/Python 3.14; macOS/Windows and both Docker architectures need new
acceptance evidence. Unsupported scheduling/permissions, derivative images, dirty source, shared
system environments, and unsafe multi-host locks defer safely under the approved policy. Automatic
updates cannot appear retroactively in old binaries; an updater-capable release must first be adopted.
Pending delivery requires available retained helper/connectivity and valid identity/version/consent.
No design guarantees recovery after simultaneous destruction of all persistent storage.

All tests in quickstart.md are instructions for future implementation; paths may be newly proposed.
Do not claim implementation, merge, deployment, live PostHog delivery or public issue filing from this
handoff. A separate implementation request is required.

## User-requested three-agent confirmation and synchronized revision

A fresh three-agent pass reviewed the actual canonical documents and existing code/test contracts:
1. Regression/requirements reviewer: preserve no-write help/dry-run/read-only behavior; keep the full
   existing offline CI and legacy-state matrix; protect JSON/CSV and MCP stdio output.
2. Devil's advocate: remove duplicate periodic senders and CLI handlers, reuse init apply logic, defer
   derivative/orchestrated image integrations, retain safety mechanisms justified by failure paths.
3. UX/minimum-build reviewer: nonblocking idle acquisition, quiet success/current/busy checks,
   deduplicated actionable notices, latest-attempt report default, avoid setup duplication.

The lead synchronized findings with all three reviewers before updating the plan. They agreed on
nonblocking lock acquisition with immediate partial-release, one helper-owned periodic sender, shared
CLI/helper handlers, init reuse, no-write exemptions and existing regression gates. The scheduler
tradeoff was escalated explicitly: user retained automatic unsupervised updates and requested manual
CLI updating for automatic-update opt-outs. Therefore minimal native registration remains; reuse an
existing supervisor where present. Manual and automatic paths use one transaction engine.

Final rechecks confirmed the agreed safety/minimality changes. The final manual-path review found two
small reconciliation items: automatic-only disable wording, and manual control dispatch must release
its ordinary shared lease before awaiting exclusive ownership. Both were corrected in contracts and
acceptance tasks. After the corrections, all three reviewers confirmed no remaining planning blockers in their assigned
regression, adversarial, and UX/minimality reviews. Final outcomes are document judgments only; no
runtime tests executed.

### Final constraints and implementation gates

- 24 tasks retained, with 31 feature-local requirements including F3 FR-015; no second updater,
  reporting daemon, scheduler framework, custom-image plugin or orchestration integration.
- Native scheduling is necessary for the user's explicit unsupervised requirement; removal was not
  silently accepted as a simplification. Permissions/unsafe installation prerequisites remain explicit.
- Preserve actual old assertions and baseline results; zero known introduced regressions is a release
  gate, not an absolute guarantee obtained from agent review. Run full existing offline CI unchanged,
  then focused acceptance across fresh/legacy state, feature switches and supported platforms.
- Busy workload sees no pre-maintenance fence/rejection; true maintenance remains exclusive and can
  produce a structured retryable response. Real MCP reconnect behavior needs client acceptance evidence.
- `auto_update=false` suppresses automatic attempts only. Explicit `bubo update` retains privacy, identity,
  migration/recovery and cross-root safety, does not enable future updates, and cannot block on its own lease.

Implementation has not started. Only planning artifacts were updated in this review pass.
