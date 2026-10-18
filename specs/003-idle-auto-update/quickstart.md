# Validation Guide — F3

Use disposable installations, state roots, local official-origin test fixtures and fake PostHog. No
real production DB/container, public issue creation or live notifications.

Run the full default regression suite from the repository root, using a Python 3.14
virtual environment with the project's test dependencies:

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q --tb=short
.venv/bin/python -m compileall -q src
.venv/bin/ruff check src/bubo tests
git diff --check
```

The current tests split bootstrap/registration coverage across
`tests/test_update_helper.py` and `tests/test_update_registration.py`; adapter,
recovery, process and platform tests use `tests/test_update_*.py` and
`tests/test_updater.py`. The original planned `test_update_bootstrap.py` and
`test_update_end_to_end.py` filenames were not used. Do not infer end-to-end native
acceptance from this naming or from passing simulated platform tests.

Native Windows checks are in `tests/test_update_windows_native.py`; Windows
bootstrap and the retained launcher are enabled. Native scheduler opt-ins are
`BUBO_NATIVE_LAUNCHD_TEST=1`, `BUBO_NATIVE_SYSTEMD_TEST=1`, and the Windows scheduler
flag documented in its native test module. These create and remove isolated user
registrations. Never infer acceptance from skips or simulated platform tests.
See `acceptance-2026-10-17.md` for native receipts, fixture limitations, and the
unexplained historical Windows rollback observation.

For documentation changes, run `npm run build` in `docs/` and inspect the generated
`docs/out/auto-update/index.html`. This builds locally and does not deploy.

1. For every safe matrix row, install A, seed reviews/config and ID X, register helper, offer B, advance
   fake schedule. Assert successful migration, B health, exact ID X, retained logical data/config, B metrics.
2. Hold CLI/MCP/poll/sync/worker leases, including child launch races; schedule update. Assert no mutation
   until idle, no admission race and no old-generation writes. Dirty/unknown/shared cases defer unchanged.
3. Inject failure/kill at every journal intent/completion point, including during recovery, helper restart,
   install, migrations and database restore. Resume helper; assert compatible code+DB, stable identity,
   F2 reports/outbox retained. Recovery failure stays fenced with usable independent helper diagnostics.
4. Disable before check, while staged, while trying idle admission and after mutation. No new automatic attempts; existing
   recovery completes safely and eligible failure delivery continues under current analytics consent.
5. Repeat Docker amd64/arm64 success/rollback with volume, ports, env and security settings preserved;
   derivative/Compose/ephemeral/unregistered scenarios defer rather than destroy customization.
6. Verify helper bootstrap/uninstall and no duplicate schedules on Linux/macOS/Windows; process/machine
   restart resumes journal before opening admission. Older updater-unaware processes cause deferral.
7. Corrupt artifacts, fake registry identity, fill disk, remove permissions, corrupt ID, lose helper/store,
   introduce schema incompatibility, and share multiple roots. Verify preflight failures and diagnostics.
8. Exercise daily/check/admission/deadline defaults with fake clock; no forced review termination and no
   repeated target retry loop. Evidence must cover every SC and matrix row before declaring full release.

9. Run existing offline CI unchanged before and after implementation, including full default pytest
   and source compilation. Retain baseline results; no newly introduced failure may pass the release gate.
10. Snapshot filesystem/output for help/version, init --dry-run, missing-DB export, read-only hot-WAL
    export and read-only MCP queries, with helper unavailable and state read-only. No bootstrap/migration/
    identity/consent-marker write; JSON/CSV and MCP stdout remain valid and unchanged.
11. Keep work continuously busy across roots: nonblocking updater tries must not fence/reject/delay it;
    immediately release partial locks. Idle acquisition still excludes racing new operations.
12. Verify successful/current/busy checks stay silent; changed blockers have one actionable notice,
    actual failures retain every report/event; report without an ID renders the latest attempt.

13. Set auto_update=false and run `bubo update`: test success, current, busy, offline, migration failure
    and recovery outcomes; configuration stays false and no automatic checks start afterward. Compare
    manual and automatic transaction coverage; other shared roots' false settings still defer.
14. With no existing supervisor, perform normal writable setup, close all CLI processes, offer a new
    release and advance scheduler clock. Assert helper checks/updates unattended; verify restart recovery
    and only one native registration. Repeat on Linux/macOS/Windows, then uninstall owned registration.
