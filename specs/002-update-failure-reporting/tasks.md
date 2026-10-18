# Tasks — F2

Fully functional; future implementation. F1 contracts required. Tests explicitly required by spec.

## Setup
- [x] T001 Define bounded failure enums and sanitized schemas in src/bubo/update_reporting.py; add tests/test_update_reporting.py covering every phase and unknown exceptions. FR-001/002/007. Done: no arbitrary strings escape into reports/events.

## US1 — Issue-ready local reports
- [x] T002 Implement separate transactional diagnostic store and Markdown renderer in src/bubo/update_reporting.py; add local `update report` command in src/bubo/cli.py. FR-001/002/003/008. Done: reports persist across application DB restore; readonly/full storage has safe fallback.
- [x] T003 Add journal reconstruction/idempotence in src/bubo/update_reporting.py and tests/test_update_reporting.py. FR-009. Done: one interrupted-phase failure per journal sequence, distinct recovery failure retained.

## US2 — Durable consent-respecting delivery
- [x] T004 Add durable outbox transport in src/bubo/update_reporting.py using F1 envelope and existing analytics_config.py consent; preserve original UUID/time/version. FR-004/005/006/007. Done: local server timeout/restart/ack-loss/opt-out scenarios pass.
- [x] T005 Implement backoff, quarantine, retry repair, capacity reservation and retention in src/bubo/update_reporting.py; test threshold, disk errors and no recursive telemetry. FR-008. Done: no silent eviction, reviews unaffected, updates defer before loss of diagnostics.

## Integration and validation
- [x] T006 Wire the sole periodic drain into the retained update helper; add fallback-only invocation drains to src/bubo/service.py with current-consent checks; document report command and failure privacy in docs/pages/telemetry.mdx. FR-001–009. Done: all quickstart scenarios and existing analytics regressions pass; F3 remains responsible for real update failure injection.

Dependencies: F1 -> T001 -> T002 -> T003 -> T004 -> T005 -> T006. US1 is independently useful
before external delivery. No concurrent edits to the shared reporting module; no task publishes issues.

T002 includes the retained-helper report/status interface when Bubo cannot start.
T005 includes `update retry-failures <attempt-id>` and helper equivalent, with consent and immutable
payload assertions; no resurrecting suppressed records. T003/T004 test protocol v1/additive records,
unknown-version quarantine and B-written records rendered/drained after rollback to A.
T006 must provide independent-helper 60-second delivery scheduling in service, CLI/cron-only, MCP-only
and Docker modes, plus bounded invocation drains where helper registration is unavailable. Validate
failure -> offline -> auto_update=false -> mode restart -> restored connectivity for each mode.

T004/T006 implement/test the helper consent handoff in contracts/failures.md: authoritative app/container
launch switches, current config, owner-only persistent veto, fail-closed unavailable policy, serialized
veto-before-dispatch, explicit policy refresh. No ambient host environment may enable container export.

Minimum-build review: T002/T005 use one helper command parser/handler implementation for both entrypoints;
report defaults to latest. T006 allows fallback invocation drains only when no helper owns periodic
sending. T004/T006 exempt help/dry-run/read-only commands from consent filesystem writes and test that
explicit writable refresh is required for independent-helper policy changes in those paths.

Implementation and fixture validation: see `../003-idle-auto-update/acceptance-2026-10-17.md`. No release or live telemetry validation is implied.
