# Update Failure Contract

Record local failure before attempting telemetry. A report contains: title `Bubo update failed: <phase>`,
UTC time, attempt ID, failure ID, installation method, installed/from/target versions, phase/code,
recovery outcome, next action, and safe reproduction context. Exclude identity values, tokens, raw
commands/environment, host paths, repository names, review/code content, raw stdout and stack traces.
Use fixed explanations indexed by error code; unknown exceptions use `unexpected_error` and phase.
This is copy/paste-ready content, not automatic issue creation.

PostHog event `update_failed`: common F1 properties plus $insert_id=failure_id, attempt_id,
phase, error_code, install_type, from_version, target_version when known, recovery_status.
Occurrence timestamp is original UTC. Enum values collapse unknowns to bounded `other` categories.
Source/target versions must be validated release/build version strings, not arbitrary error text.
Existing configured endpoint/analytics opt-outs apply; OTLP configuration does not enable PostHog.

An external Docker controller uses the same persisted payload and identity snapshot rather than
creating its own installation ID. Its own software version may be separate local diagnostic metadata;
bubo_version is the installed Bubo version at the failed phase, supplied by the attempt journal.
No valid identity/version: local report only and update deferred, never fabricate identifiers.

Proposed CLI `bubo update report <attempt-id>` renders Markdown from local diagnostics without network.
`auto_update=false` does not discard eligible pending failures. Opt-out does discard their export payloads.

## Operator recovery and independent helper

`bubo update status` lists pending/quarantined failures and attempt IDs locally.
`bubo update retry-failures <attempt-id>` revalidates current consent and endpoint configuration, then
requeues only quarantined eligible records with unchanged event ID/time/version/identity. It performs
no update and cannot revive suppressed records. No endpoint/credential override from failure data.
The retained `bubo-update-helper report|status|retry-failures` interface supplies equivalent diagnostics
when the Bubo package is broken; installation registers this stable executable outside replacement.
Log an exact report command, not a nonexistent Markdown file path. Rendering never requires network.

All records have protocol_version=1. Changes within v1 are additive; old readers ignore unknown fields
and retain immutable payloads. Incompatible protocol changes require a retained compatible helper
before activation, otherwise defer. Never drop/misinterpret unknown-version records; keep them
quarantined with a usable independent-helper diagnostic. Helper compatibility spans rollback releases.

The independent helper owns a 60-second delivery wakeup even with auto_update=false, across service,
CLI/cron-only, MCP-only and Docker deployments. Analytics opt-out disables export, not local reports.
When no resident helper can be registered, startup/exit of CLI/service/MCP and explicit retry command
provide bounded local drains; external delivery then requires a later invocation. Such an installation
cannot start automatic updates until the independent helper prerequisite is satisfied, so new updater
failures are never created without a surviving delivery owner. Offline+disabled-update restart tests
must cover each operating mode and preserve pending payloads.

## Consent across independently launched helpers

Existing analytics precedence remains DO_NOT_TRACK, then BUBO_ANALYTICS, then [analytics], with blank
destination/key disabling export. Helper registration captures only the two nonsecret analytics switches
from the authoritative application launch environment, with absent explicitly represented; Docker
controller obtains them from its registered container/deployment descriptor, not its host environment.
The helper re-resolves current registered launch settings and current analytics config before every send;
its own opt-out can additionally veto but never override the application's opt-out. Unreadable, changed
but unvalidated, ambiguous or unavailable consent source fails closed (hold eligible payload, no send).
Do not export the rest of the environment or copy secret values into the journal/consent record.

A writable Bubo invocation discovering an environment opt-out writes an owner-only helper export veto before
any update handling and invalidates pending export per FR-005. This veto is scoped to the update-failure
helper, not ordinary independent OTLP or unrelated analytics behavior. It persists across application
exit/rollback. Clearing it requires explicit `bubo update configure` re-registration of the effective
analytics policy; mere absence of DO_NOT_TRACK in the helper environment is not opt-in. Document this
step alongside existing privacy controls. Capture-time false never creates a later exportable payload.
An attempted send is serialized with consent-veto updates; a request already accepted by the network
cannot be recalled. Tests synchronize veto-before-dispatch, not an impossible recall of in-flight events.

T004/T006 must test application/container opt-out versus helper-enabled environment, config changes,
missing launch policy, queued payload then environment veto, and explicit policy refresh. F3 T002
owns registration and `update configure`; F2 owns evaluation/veto and delivery checks.

## Minimum control surface and delivery ownership

The retained helper is the sole periodic sender. Service/CLI/MCP bounded invocation drains are fallback
only when no managed helper owns delivery; do not run duplicate independent periodic send loops.
Normal `bubo update status|report|retry-failures` dispatches to the retained helper's parser/handlers and
store, not a second implementation. `bubo update report` with no ID renders the latest attempt; the
explicit ID remains available. All reporting is local and available through the retained executable if
Bubo itself cannot start. No additional reporting daemon, plugin API or diagnostic store is required.

Help, dry-run and existing read-only commands never persist consent markers or register helpers.
Their local opt-out remains effective for that invocation, but updating the independent helper's policy
requires the explicit writable refresh command; document that boundary. Reuse existing init apply code
for registration/refresh, never silently clear a veto during ordinary initialization.
