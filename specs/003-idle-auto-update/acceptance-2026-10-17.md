# Platform acceptance — 2026-10-17

Status: updater implementation and isolated native acceptance are complete, with
the historical uncertainty below retained explicitly. Windows bootstrap is enabled.
These fixture tests do not prove published-release acceptance or authorize rollout.

## Verified

- macOS regression suite: **1,543 passed, 39 skipped, 10 deselected** in 100.10s.
  The ten deselections are the configured live integration exclusion. Windows-native
  tests do not execute on macOS. Analytics was disabled for remote acceptance.
- Native Windows, Administrator@192.168.1.104, isolated Python 3.14.8:
  **34 passed** in 198.75s, with temporary Task Scheduler acceptance enabled.
  This includes native process/Job ownership, detached-worker survival, scheduler
  lifecycle with a Unicode path, startup receipts, directory durability, bridge
  handoff during environment replacement, and actual pip package transactions.
- Windows package transactions use local deterministic wheels, production
  `PythonInstaller` and `UpdateEngine`, and a real SQLite migration. Success and
  post-install/post-migration rollback preserve the original data and installation
  UUID; rollback restores both the earlier executable version and database schema.
- Native real MCP coverage uses the generated retained-launcher configuration,
  initializes, lists tools, performs a database-backed health request, reconnects,
  and verifies both supervisors close. The full-package cases also replace the
  package during an idle MCP session and reconnect following success or rollback.
- Native crash recovery kills the isolated updater after a completed migration.
  A separate process restores both package and database, preserves the installation
  UUID, and releases maintenance only after successful recovery.
- Focused production installer/recovery tests: **70 passed** on Linux and macOS,
  including pip, uv and pipx temporary environments. Linux uses an unprivileged
  process and isolated current pipx/uv tools.
- All final tests disable product analytics and OpenTelemetry by default. External
  PostHog/OTLP transports are blocked in the test harness; telemetry-specific unit
  tests use explicit fake/in-memory transports. **191 focused telemetry tests passed.**
  Installed product analytics still defaults to enabled with manual opt-out.
- Ruff, Python compilation, and `git diff --check` passed after the implementation
  and test fixture corrections.

## Review and remaining gates

Three reviewers checked requirements, minimal user experience, and adversarial
safety. The adversarial review found no concrete blocker in the Windows bridge
handoff: durable maintenance intent and exclusive installation/state admission
precede release of the configuration handle. Transient legacy bridge readers
remain a conservative rename/deferral condition.

Additional verified checks:

- Windows legacy registered/unregistered processes retain admission for their
  lifetime; native first-registration scanning, actual venv bootstrap, repeat
  configure and temporary scheduled-task removal passed.
- Windows shutdown now pins process handles before graceful stop and waits for
  native exit even when Job accounting reports zero. Focused Windows
  process/resident/platform checks: **91 passed**.
- Native Linux user-systemd service registration, execution while no CLI is
  running, and removal: **1 passed** in 0.51s. This used WSL's existing root user
  service manager. A subsequent native ARM64 Linux run under an ordinary user
  also passed (**1 test**, 0.66s).
- Isolated real Docker 28.5.1 on Linux amd64, private loopback registry, actual Bubo
  source in two fixture-version images: **3 passed** in 33.00s for package replacement,
  post-migration matched rollback, and actual updater-process-death recovery. Container and controller run as UID 10001.
  Persistent data, installation identity, restart/network settings and fence
  removal were checked. Docker's valid null changes response is now accepted;
  unknown/nonempty writable-layer changes still defer. **109 focused Docker
  regressions passed**. The isolated daemon was stopped after acceptance.
- Real clean-editable upgrade/restore, interrupted swaps, and modified-source
  preservation: **10 native Windows tests passed** in 118.78s. Native testing
  found/fixed Git path normalization and flushing read-only staged Git objects.
  Source manifests now use read-only handles; staged flushes restore permissions
  even on failure. **32 installer/source regressions passed** on macOS in 63.26s,
  followed by focused read-only and ownership checks.

- Current unprivileged Linux regression passes: **1,534 passed, 37 skipped,
  10 deselected** in 164.21s; platform skips remain separate gates.
- Native Docker ARM64 acceptance: **3 passed** in 20.14s on an actual ARM64 Linux
  VM (architecture verified), covering update, matched rollback, and updater death.
  A first concurrent-load run safely rolled back one readiness failure; the quiet
  rerun passed. The daemon was stopped after tests. Fixture-version metadata is
  not published-release acceptance. Native Windows uv/pipx upgrade and external
  launcher rollback now pass: **10 tests** in 14.63s (uv, pipx/pip, pipx/uv plus
  interruption/ownership cases). Copies require exact environment bytes; links
  require exact environment targets. Recovery preserves evidence before swapping
  environments so interruption can resume; operator-modified launchers defer.

- Native ARM64 Linux full regression under an ordinary user: **1,544 passed,
  38 skipped, 10 deselected** in 65.08s.
- Native macOS LaunchAgent registration, duplicate registration, background
  execution, and removal: **1 passed** in 0.72s. The helper writes a local receipt;
  it performs no discovery, package update, or telemetry operation.
- Native Windows real executable-lock recovery: **1 passed**, 34 deselected in
  11.39s. A real non-delete-sharing handle forces blocked restoration; maintenance
  and local diagnostics persist, and recovery succeeds after releasing the lock.
  This proves safe handling of that condition, not the historical incident's cause.

## Unresolved observation and limits

One full Windows MCP rollback run returned `recovery_blocked` after the handle-wait
fix, without capturing its underlying exception. Subsequent full runs, three
consecutive update/rollback pairs (six cases), and three concurrent rollback cases
passed. Test-only traceback capture is now installed for recurrence. No specific
cause is established; passing reruns do not explain it. The final adversarial
review found no additional concrete blocker. Production diagnostics stay sanitized.

The initial ARM64 Docker run under concurrent heavy load safely rolled back a
readiness failure; its quiet rerun passed. Native package/image fixtures use actual
source with artificial version metadata, not a published-release upgrade.
The temporary Docker daemons were stopped and the ARM64 VM was deleted.

## Single-setting telemetry follow-up

Implemented the requested follow-up after updater acceptance: `[analytics].enabled`
is the single default-on consent setting. Bubo-owned OTel providers export only
fixed-name numeric measurements and spans as sanitized PostHog events through the
shared version/identity transport. Consent is controlled by `[analytics].enabled`.
Development/test switches suppress analytics, OTel and retained-helper failure
reporting. Pending provider data and queued events are revoked on configuration
change. No live telemetry ingestion was tested.

Final consolidation validation (separate from the earlier updater receipts):
- macOS full regression: **1,553 passed, 41 skipped, 10 deselected** in 96.45s.
  Receipt: `/tmp/bubo-macos-consolidated-final.txt`.
- Ruff, compileall, and `git diff --check`: passed.
- Documentation production build: passed, including static generation and postbuild.
  Receipt: `/tmp/bubo-docs-final-build.txt`.
- Three reviewers checked implementation/requirements, adversarial behavior, and
  minimum scope/user documentation. The consent-reload findings were fixed and
  the adversarial recheck found no new concrete blocker.
- All test delivery used disabled external telemetry or fake/in-memory capture.
  Native Windows consolidated rerun is recorded below.

Test-harness follow-up: an overbroad Windows selection included POSIX-only helper
fixtures (`os.getuid`, POSIX process listings) and a Linux `/proc` unwritable-path
fixture. That run was **4 failed, 151 passed, 2 skipped, 36 errors**; none of its
failures were native updater scenarios. Shared ownership/consent fixtures now use
`current_owner()`, and unwritable identity uses a temporary regular-file parent.
The shared-fixture macOS rerun passed **78 tests**. POSIX scheduler/process-list
simulations are not claimed as Windows acceptance; native Windows tests exercise
its actual mechanisms. The corrected targeted Windows receipt follows.

Final Windows consolidated run: **139 passed, 2 skipped** in 186.10s, covering
native updater/MCP/matched rollback/executable-lock recovery and portable telemetry
contracts. Separate retained-helper consent selection: **8 passed, 44 deselected**
in 1.18s. Receipts are `final-consolidated-results.txt` and
`final-consent-results.txt` under the isolated Windows test root. These are targeted
Windows results, not a claim of a complete Windows-wide test suite.
The accidental Linux-path fixture files created on Windows were inventoried and
removed; no unrelated paths were removed. Latest template default check: **10
passed**, with the explicit shipping `analytics.enabled = true`.

Implementation and requested telemetry follow-up are complete for review. No commit,
push, production rollout, or live PostHog ingestion test was performed. The earlier
unexplained Windows observation remains a disclosed uncertainty, not a demonstrated
current defect or a cause explained by later tests.

## Latest user-directed follow-up: one setting, OTel context, live metrics

`[analytics].enabled` is the sole consent setting. Bubo owns the OTel provider
lifecycle. Dev/test fixtures disable their configuration;
all intentional capture tests use fake transport. Installed version, persistent
identity, model/family, canonical languages, OS/architecture/runtime and safe
classification dimensions are retained in emitted events.

Expanded metrics cover the six SQLite application tables with explicit cohorts
and denominators. Live mode is a read-only loopback token URL with bounded queries;
its process holds an update-admission lease until exit. Potential ROI is an
editable assumption-based scenario, and governance evidence is not certification.

- Full macOS regression: **1,577 passed, 41 skipped, 10 deselected**, 99.44s.
  `/tmp/bubo-single-config-dashboard-final.txt`
- After bounded-query fix: **73 focused tests passed**, 3.74s.
  `/tmp/bubo-final-followup.txt`
- UI build, docs production build, Ruff and whitespace checks passed.
- Browser checks: desktop/mobile, live SQLite refresh, period filters, deterministic
  ROI result, empty database and disconnected server; temporary servers stopped.
- lean-ctx/RTK integration was researched only. No tool installation/configuration
  changes, release/push, or live telemetry ingestion were performed.

Final native Windows follow-up for the single-setting/context change:
**157 passed, 2 skipped** in 189.77s, plus **6 helper-consent tests passed**,
46 deselected, in 5.10s. Test setup used disabled configuration and fake transports;
legacy environment switches were not used to silence telemetry. These are targeted
Windows receipts, not a claim that every test in the repository ran on Windows.

Research artifact: `/tmp/bubo-context-tools-research.md`. Dashboard sample:
`/tmp/bubo-dashboard-expanded-preview/index.html`. No integration of lean-ctx/RTK
was performed; installed tooling was distinguished from verified runtime use.

### Legacy telemetry controls removed

Removed obsolete helper policy records, veto-file cleanup, environment-flag
fixtures, and ignored-override tests. The sole preference is `[analytics].enabled`;
it defaults to true for installations. Test runtime configs disable analytics,
and consent tests use guarded fake transports.

Validation after cleanup: telemetry/config/context/reporting/version suite
**168 passed** (1.10s); updater/helper/Docker setup/readiness suite **183 passed**
(16.75s). Ruff and whitespace checks passed. No external telemetry was sent.
