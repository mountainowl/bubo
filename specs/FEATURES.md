# Seamless Bubo OSS updates

Date: 2026-10-16. Delivery mode: **fully functional for every feature**.
Canonical requirements and decisions for this feature set; implementation has not started.

## Settled requirements

- R1: OSS Bubo automatically updates by default only when not in use.
- R2: Database migration is automatic and the successful journey requires no manual repair.
- R3: Every update failure has a redacted, copy/paste-ready Bubo issue report.
- R4: Every update failure is durably queued for PostHog when analytics is enabled; existing opt-outs apply.
- R5: All PostHog event/property data and emitted metric data identify the installed Bubo version.
- R6: Installation identity and distinct identity remain unchanged across update and recovery.
- R7: `auto_update` defaults to true; false disables automatic updating, independently of analytics consent.
- R9: Explicit `bubo update` remains available with auto_update=false, using the same safe workflow.
  User retained unattended updates for unsupervised installs, with minimal native background setup.
- R8: All installation types are in scope: uv tool, pipx, pip, Docker, source/editable.
  User approved an external Docker controller and safe deferral for permissions, local modifications,
  or unsupported safe replacement. No automatic privilege escalation or overwriting user changes.

## Features and dependency order

| ID | Outcome / specification | Mode | Baseline and requested delta | Scope / exclusions | Acceptance | Depends on |
|---|---|---|---|---|---|---|
| F1 | [Version attribution and stable identity](001-telemetry-version-identity/spec.md) | Fully functional | PostHog already has common version/IDs; add complete metrics coverage and upgrade-safe identity contract | All emitted metrics/events; no retroactive rewriting of historical events | All channel captures carry producer version; IDs identical across recovery | None |
| F2 | [Actionable update failures](002-update-failure-reporting/spec.md) | Fully functional | Current analytics is best-effort and drops failed sends; add local reports and durable update-failure queue | Update lifecycle failures; no issue submission or general analytics rewrite | Every injected failure recorded; offline retry and opt-out verified | F1 |
| F3 | [Idle automatic updates and DB recovery](003-idle-auto-update/spec.md) | Fully functional | Additive DB migration exists; no verified automatic updater | All named installs with approved safe deferrals; no custom-image overwrite or privilege escalation | Idle admission, update, migration, health check, restart/recovery across install matrix | F1, F2 |

Infrastructure belongs under its consuming feature. F1/F2 may be validated independently with synthetic
upgrade/failure fixtures; F3 supplies their real end-to-end update integration.

## User decisions, 2026-10-16

Fully functional scope; initialize Spec Kit in bubo-oss; adopt constitution principles;
all installation types including Docker; honor analytics opt-out; auto_update false disables automatic updates;
external Docker controller plus safe deferral approved. Proposed technical defaults and evidence
are recorded in each research.md, rather than a second product decision ledger.

## Workflow and readiness

Installed Specify/Clarify/Plan/Tasks/Analyze instructions were read. No extension hooks are installed.
The installed feature setup scripts only write planning state; no Git branch, commit, implementation,
publishing or external notification is performed. Review results: see [analysis](ANALYSIS.md).

## Three-agent refinement, 2026-10-16

Requested priorities: minimum build-out, zero introduced regressions, best user experience.
Agreed reductions: nonblocking idle acquisition; one helper-owned periodic sender; one command-handler
implementation; existing init reuse; no derivative/orchestrator update integration; quiet success/busy
checks and deduplicated actionable notices. Retain every genuine failure report/event.
User explicitly retained unsupervised unattended updates and added manual CLI updates for automatic-
update opt-outs; native background registration therefore remains necessary. See analysis for synchronized
review outcomes, concrete regression gates and required tests. No implementation has begun.
