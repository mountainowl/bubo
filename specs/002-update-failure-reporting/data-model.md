# Data Model — F2

Failure: failure_id UUID, attempt_id UUID, occurred_at UTC, phase enum, error_code enum, install_type,
producer_version, from_version, target_version optional, recovery_status, sanitized_action_code.
Issue report renderer derives a Markdown title/body from these fields; no raw subprocess output stored.
Delivery: failure_id FK, immutable payload bytes, original $insert_id, captured identity/version/time,
status pending/acknowledged/quarantined/suppressed, attempts, next_attempt_at, last_transport_code.
Failure + eligible delivery insert in one transaction; unique failure_id makes recovery idempotent.

Transitions: capture -> pending -> acknowledged; pending -> retry -> pending; pending -> quarantined
on permanent rejection; pending/quarantined -> suppressed and payload erased on opt-out.
Opted-out capture never creates an exportable payload. Record retention never revives suppressed events.
Crash discovery records interrupted phase once using journal attempt/phase sequence identity.

Protocol/schema version 1 is shared with the independent retained helper. Readers tolerate additive
fields and preserve unknown payload data. Unsupported versions remain quarantined, not decoded or
silently deleted. Retain the compatible helper across package rollback until its records are handled.
