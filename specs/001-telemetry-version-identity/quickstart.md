# Validation Guide — F1 (future implementation)

Prerequisites: isolated Python 3.14+ environment, test collectors, temporary state root. Never use live PostHog.
Planned tests below must exist before running these commands; they are not implemented yet.

`PYTHONPATH=src .venv/bin/python -m pytest tests/test_analytics.py tests/test_telemetry_metrics.py tests/test_update_identity.py`

1. Enumerate all exported event/metric shapes; run each emitter and assert producer-version coverage.
2. Capture A payload, switch test runtime to B, replay A: version remains A; newly produced events show B.
3. Snapshot valid ID, simulate success/restart/rollback/volume reuse: both identity fields and file unchanged.
4. Remove/corrupt identity after snapshot: activation blocked, no fresh identity or external event.
5. Opt out: no external analytics; local metric behavior follows existing telemetry configuration.
6. Test metadata resolution for packaged, editable, unavailable metadata and absent initial identity.
Expected: SC-001–003, with no historical-data rewrite and no live external side effects.

7. Exercise both consent switches in all combinations; missing real version suppresses external emission.
8. Race identity initialization against an update guard, kill/restart the holder, and assert no new ID.
