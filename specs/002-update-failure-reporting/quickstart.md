# Validation Guide — F2 (future implementation)

Prerequisites: isolated state directory, local fake PostHog HTTP server, no production credentials.
Planned command after tests exist:
`PYTHONPATH=src .venv/bin/python -m pytest tests/test_update_reporting.py tests/test_analytics.py`

1. Inject a failure for every lifecycle phase; render `bubo update report <attempt-id>` and paste into a
   local Markdown preview. Confirm actionable content and no seeded secrets/private paths/content.
2. Return timeout/429/500, restart the reporter, then accept: immutable ID/time/version and retained event.
3. Lose the response after acceptance: retransmit the same $insert_id, without claiming exactly once.
4. Return 400: quarantine visibly without tight retries; fix config and explicitly retry.
5. Opt out at capture and before retry, then opt in: suppressed failures are not backfilled.
6. Simulate DB rollback, helper kill, store full/read-only/corrupt, capacity threshold and report rendering
   failure: diagnostics or fallback local warning survive; new updates defer, ordinary reviews continue.
7. Ensure telemetry transport failures update local delivery status rather than recursively adding events.
Expected SC-001–004. Real updater integrations are subsequently validated by F3.

8. Run `bubo update status`, then `bubo update retry-failures <attempt-id>` after fixing a permanent
   rejection. Current consent is checked and original event IDs/time/version remain unchanged.
9. Break the application package and use `bubo-update-helper report <attempt-id>`; render/drain newer
   additive records after rollback, quarantine unsupported protocol without data loss.
10. Repeat offline failure -> disable auto_update -> restart -> restore network across CLI/cron-only,
    service, MCP-only and Docker. Registered helper delivers without restarting automatic updates.
