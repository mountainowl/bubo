# Research — F2

Evidence: src/bubo/analytics.py:515–546 clears pending events before sending and ignores transport errors.
Existing allowlists at analytics.py:305–361 deliberately exclude arbitrary strings. analytics_config.py
provides the existing consent contract. These are baseline facts, not durable delivery implementation.

Decision: separate diagnostic SQLite store under persistent state, not the application DB. Rationale:
DB rollback must not erase the failure that triggered it. Standard SQLite transactions avoid partial
outbox/report relationships. Render Markdown on demand from sanitized records, with the exact report-rendering command printed to local logs. Alternative rejected: migration-era application table.

Decision: at-least-once delivery using stable event UUID and PostHog $insert_id property. Validate the
configured ingestion endpoint accepts the envelope; do not claim endpoint-side exact deduplication.
Acknowledgement loss retains pending state. HTTP 2xx acknowledges only transport acceptance, not
proof of downstream dashboard processing. Nonretryable 4xx stays locally quarantined and actionable;
429/5xx/timeouts retry. Repaired configuration may explicitly requeue a quarantined payload.

Decision: bounded diagnostic capacity of 100 MiB with 10 MiB reserved for recovery diagnostics.
Prune only acknowledged/local-only records older than 30 days; never evict unacknowledged events.
Check capacity before any new update; if unavailable, defer safely and log a bounded local warning.
A failure event up to 16 KiB and report up to 64 KiB is sufficient using category codes and sanitized
summaries. Do not recursively enqueue delivery failures; update the existing delivery diagnostic.
These are engineering defaults, validated by boundary tests rather than new user-facing knobs.
