# Update and Admission Contract

Configuration uses existing TOML syntax: `auto_update = true` at the top level of config/env.toml.
User's `auto_update: true` expresses the boolean requirement, not a request to introduce YAML.
Omitted means true, false suppresses new automatic checks/staging/mutation. Invalid value is an actionable config
error. For automatic triggers, recheck effective config before fencing and before mutation; after mutation begins recover/commit
safely even if disabled. Status/report/retry-failures are local diagnostic commands defined by F2.

Every ordinary Bubo CLI data/runtime operation, MCP request, poll, sync and worker holds a shared lease for its
complete operation. Status/report can read immutable diagnostics without the application DB. Worker
launch transfers ownership with overlapping parent/child leases and an acknowledgement; orphan
handoff is not mistaken for idle. Poll/service waiting and inactive MCP transport are not operations.
After a committed update, idle MCP stdio processes exit with retryable status 75. Clients must
reconnect and launch the updated process. Active requests finish before the transaction starts;
the restart watcher emits no stdout protocol bytes and waits for the maintenance fence to clear.
Updater makes a nonblocking exclusive-acquisition attempt using the same operation-lease primitive,
across the installation and all registered roots in canonical order. Hold the installation registry guard
so new root registrations cannot race. If any lease is busy, immediately release every partial acquisition
and defer without fencing or delaying ordinary work. Only after all exclusive locks are held may it
persist the maintenance fence. No pre-maintenance drain wait or repeated busy-work rejection is allowed. New callers during maintenance get a clear retryable maintenance response, never silent
loss or partial writes; queued background work resumes after reopening. Human interactive work is
not auto-replayed where that could duplicate side effects.

Under exclusive ownership: verify identity/version and diagnostic capacity; retain artifact+dependency
rollback material; consistent DB backup; install/activate target; run migrations with validated new
runtime; integrity and representative read/write health probe in an isolated transaction rolled back;
restart/re-exec idle residents into new generation; persist commit; release guards. Health checks must
not send SCM comments, trigger reviews, or emit production test telemetry. MCP reconnects use existing
client behavior and explicit retryable maintenance signaling; no false claim of transport continuity.

Helper authenticates local control through owner-only state/IPC; invoke manager arguments as arrays,
reject symlink/path escapes and arbitrary executable overrides from release metadata. Record intent
before mutation, retain trusted helper and artifacts throughout recovery. On failure restore code,
dependencies and DB as a compatible pair; revalidate identity and health before reopening. A failed
recovery remains visibly blocked and retries only through a bounded, idempotent recovery procedure.
Uncoordinated processes/roots or unsupported lock semantics cause pre-mutation deferral.

## Resolved aggregate safety contracts

Automatic shared-code replacement: require every registered state root to have effective auto_update=true immediately
before fencing and again before mutation. Any false, unreadable, unknown or concurrently changed
configuration defers the whole shared-code update. Hold configuration-generation snapshots and
revalidate them; do not let root A replace code used by opted-out root B. Unregistered roots/processes
cause deferral. T001/T003/T012 must cover conflicting roots and concurrent toggle races.

Deadline/cancellation: the 10-minute watchdog requests cancellation, never directly starts restoration.
The helper tracks mutation process groups/Windows job objects and descendants, using process-start
identities. Before restoring any artifact/DB, establish that every installer/migration writer has exited
and been reaped or is otherwise conclusively fenced from those resources. If writer quiescence cannot
be proved after helper death/timeout, remain in blocked recovery with admission closed and diagnostics;
do not race a late writer. T007/T012 cover hung descendants, delayed writes and helper death.

Resident startup: restart residents in a restricted maintenance-generation readiness mode while the
helper holds exclusive admission. In this mode they load code and establish transport/readiness only;
no application DB open/migration, normal work admission or external side effect is permitted. The helper
alone performs the new-runtime migration/health probe through a scoped exclusive-owner capability.
Residents report version/generation ready, then wait for the durable commit and fence release before
normal startup operations acquire shared leases. An unforgeable owner-local capability is never exposed
to normal requests. This prevents waiting for a new process that is itself waiting for the updater lock.
Readiness failure recovers before admission opens. T004/T007/T012 test service and MCP held-fence restart.

## Source activation

Prepare a complete replacement checkout outside the live source and retain the original directory
across activation. Both directory rename boundaries must recover after process death. Include ignored
user files in preparation and collision checks; use `--no-overwrite-ignore` only in the prepared copy.
Nested environments/controllers, cross-filesystem source swaps, submodules and linked worktrees safely
defer until their layout can be restored without losing local state. Restore the original branch and
the dependency environment together. Never overwrite tracked or ignored user changes during rollback.

## No-regression and quiet-operation contract

Help/version, init --dry-run, missing-DB reports, read-only exports and read-only MCP queries MUST keep
existing no-bootstrap/no-migration/no-write behavior. They must not register a helper, create identity,
write a consent veto, or require a writable state directory. Read-only activity can participate only
through coordination that preserves those guarantees; if update exclusion cannot be proved for such
an operation, defer the update instead of weakening the read-only contract. Preserve hot-WAL read-only
exports. Registration and normal mutating startup are the only automatic bootstrap opportunities.

Do not interpret an opt-out environment observed solely in a no-write invocation as a persisted policy
change to an independent helper; explicit policy refresh remains the writable revocation interface.
The invocation itself still honors its own privacy controls. This limitation must be documented.

Successful/current/busy checks are silent. Busy is status-only, not a failed update or PostHog failure.
A changed actionable blocker gets one deduplicated local notice with a concrete next action; every actual
failure still gets its own durable report/eligible event. Deduplication applies to notices, not failure data.
Keep CLI JSON/CSV output and MCP stdio protocol unchanged. Notices go only to existing logs/stderr;
MCP maintenance uses a valid structured protocol error, never unsolicited bytes on stdout.

## Manual update and unattended setup

`bubo update` explicitly starts one update for the resolved installation; subcommands status/report/
retry-failures/configure retain their diagnostic/control meanings. No command means no manual request.
Manual and automatic paths call the same transaction entrypoint with trigger=manual|automatic.
Only automatic trigger checks the initiating root's auto_update boolean. Manual does not rewrite config,
create a recurring opt-in or bypass analytics consent, idle admission, integrity or recovery checks.
For shared code, a manual initiating root may override its own false setting; any other registered root
with false/unreadable/unknown policy still defers replacement. Record initiating root/trigger locally.

Exit 0 means updated successfully or already current; exit 75 means safely deferred (busy/prerequisite),
exit 1 means failure with recovery outcome and a usable local report command. Busy never force-kills work.
No duplicate download/migration/recovery implementation for CLI. A manual attempt uses the retained
helper even when automatic scheduling is disabled, and recovery runs before admitting later work.

Prefer an existing supervisor; otherwise register one narrowly scoped native user-level helper service
using systemd/LaunchAgent/Task Scheduler as appropriate. Reuse one helper loop and one registration
implementation through existing writable init/setup; no second daemon or custom scheduler framework.
No bootstrap from help/dry-run/read-only commands. Unsupported permission/platform conditions remain
explicit prerequisites. A normal writable startup/init must establish registration or show one actionable
setup notice. Automatic checks must continue while CLI is closed; test all targeted native platforms.
Auto_update=false stops automatic discovery/replacement, while helper recovery and consent-permitted
failure delivery remain available. Uninstall removes only Bubo-owned registration.

Manual update control dispatch MUST NOT hold an ordinary shared activity lease while awaiting its
helper's exclusive admission. Resolve/validate the target, release any temporary discovery lease, then
delegate. The helper revalidates the target under its installation guard before acquiring exclusive
ownership. This exception permits no direct application DB access by the control dispatcher and does
not exempt real CLI work from leases. T008/T012 must execute the actual CLI command while otherwise
idle and prove it succeeds rather than self-deferring, including a concurrent-work deferral case.

## Fenced resident startup handshake

A retained supervisor launches a candidate with `BUBO_UPDATE_START_REQUEST=<UUID>`.
The candidate consumes that environment variable and reads the owner-controlled
`residents/<UUID>.json` from its registered installation controller. Protocol version 1
binds the request to attempt ID, fence token, actual state directory, resident kind
(`service`, `mcp-http`, `mcp-stdio`), installed release version and intended terminal
outcome (`committed` or `restored`). Unknown installed versions reject startup.

After real startup resources are acquired, an exclusive durable claim prevents reuse.
The candidate writes `<UUID>.ready.json` with PID, process-start identity and loaded
version. The supervisor MUST verify this is its launched, still-live process; a readiness
file alone is insufficient. A crashed or rejected candidate requires a fresh UUID.

Service readiness includes service/monitor locks and parsed configuration, with polling
and database work parked. HTTP readiness follows successful socket binding; tools remain
fenced. Stdio readiness follows entry into the transport lifespan, before completing the
MCP initialization exchange. It is not a claim that clients can perform application work
before commit. Commit release revalidates the terminal journal and loaded generation
under shared admission locks. Fence disappearance alone never releases the gate.

Changed attempts, failed restoration, timeouts and stop requests reject the candidate.
HTTP performs normal server shutdown and releases sockets. Stdio exits 75 directly when
rejected, since FastMCP's blocking stdin reader can otherwise prevent termination while
the client keeps stdin open. No application work has been admitted in this state. The
client must reconnect. Normal startup without a request does not use this handshake or
write readiness artifacts.

Implementation status: resident-side hooks, controller receipt validation, transaction
lifecycle hooks and POSIX retained-parent discovery/control are implemented. Native
scheduler/platform acceptance and the remaining installation/platform paths are not complete.

Controller receipt validation is implemented by `ResidentReadiness`: issue a fresh
request only inside the matching fenced phase, bind the actual retained `Popen`
child handle, durably record PID/start identity without environment credentials,
and revalidate the live child and complete receipt immediately before commit.
The child environment removes `BUBO_MAINTENANCE_TOKEN`. A receipt without an owned
child, a changed target/source version, PID reuse, child death or changed attempt
rejects readiness. `ResidentSupervisor` owns termination/reaping; `ResidentController`
connects its attempt-bound acknowledgements to the update engine.

`UpdateEngine` accepts a resident controller and durably records that requirement.
Under exclusive admission, it quiesces residents before backup, then starts and verifies
the candidate after migration and before committing. Recovery proves candidate shutdown
before restoring code or data, and verifies restored residents before reopening admission.
A missing required controller or unproven shutdown keeps recovery fenced.

Durable `committed` and `restored` decisions survive finalization and context-manager
teardown errors. Recovery of either terminal decision restarts/verifies that generation;
it must never reinterpret cleanup failure as permission to roll back a committed update.
Successful terminal recovery reports `updated` for a commit and `restored` for rollback,
refreshes local diagnostics and CLI status, and preserves previously captured PostHog
payloads. Real subprocess tests cover commit-gated work and candidate death/reaping before
restoration.

Registered POSIX service/MCP entrypoints exec a retained, standard-library-only helper
parent. The parent preserves the original invocation/environment in memory, launches a
child in its own group and publishes parent/child process identity without credentials.
Replacement children reopen the original working-directory pathname, including after an
editable checkout swap; they do not inherit a parent directory inode moved into a backup.
The helper snapshots parent inventory into the attempt journal before stopping anything.
Parents validate every mailbox command against the current attempt and maintenance fence.
Readiness acknowledgements require the parent's actual live child handle; loss of the
parent fails closed rather than reconstructing credentials or signalling an unowned PID.

Exit observation uses `waitid(WNOWAIT)` so a leader PID cannot be reused before group
shutdown. Maintenance waits for stable stopped-member censuses, defers if a visible
descendant escaped the group, kills the frozen group, and reaps only after no live group
members remain. Separately, exclusive update admission proves that detached Bubo writers
have finished: inherited shared lease descriptors survive `setsid`, double fork and
reparenting. Bounded reviewer subprocesses also inherit those descriptors, so killing
an admitted worker cannot expose its still-running reviewer to an update. This does not
claim containment of arbitrary foreign, unleased daemons.

An initialized stdio MCP session is never reused for a replacement server. The parent
initializes the replacement on private pipes, verifies readiness under the fence, then
closes the original connection with exit 75 after commit/restoration. Clients reconnect.
Service and HTTP replacements retain their normal transport behavior after the gate opens.
If retained-helper setup is unavailable, the original resident can continue while lifetime
shared leases prevent unsafe replacement; existing registered-controller failures produce
local/outbox diagnostics honoring analytics policy.

Real-process coverage includes retained-archive replacement and rollback, an initialized
stdio session's private replacement probe and reconnect exit, unreaped-leader ownership,
same-group descendant shutdown, escaped-descendant deferral, and double-fork writer leases.
Parent-loss recovery remains a safe deferral; automatic credential-preserving reattachment
after parent death is not certified by these tests.

Actual retained-helper service and authenticated HTTP MCP processes additionally exercise
commit and installer-failure rollback, including socket/singleton release and startup gating.
These four scenarios use fixture code activation rather than published package installation.

Docker external registration now maps canonical host journal paths to container-local roots,
rejects lexical and symlink escapes, shares admission lock paths, and suppresses native
bootstrap inside the container. Read-only preflight rejects incompatible runtime UID/userns,
custom writable layers, namespace/link dependencies, shared persistent mounts (including
workspaces outside Bubo's root), executable/import overlays and custom Python loading paths.
Network gateway priority survives descriptor reconstruction. Fixture tests verify these
contracts; real daemon mount propagation and namespace ownership remain unverified.

The Docker replacement controller, daemon-bound readiness, migration/rollback integration,
host scheduling and terminal restart-policy restoration are still unimplemented. Revalidate
daemon identity/security/defaults and dependency inventory immediately before mutation;
the current read-only inventory is not an ownership lock. Docker acceptance must not be
claimed from these fixtures. Latest full regression: 1018 passed, 10 deselected; subsequent
Docker/registration adversarial regression: 70 passed. Windows, parent-loss recovery,
bounded retention and native scheduler acceptance also remain open.

Docker image staging now accepts only a pinned upstream digest and matching platform.
An isolated read-only probe checks the image's installed Bubo version without user-data
mounts, network access, application credentials or captured logs. Durable probe identity
and create/removal intents recover lost create/delete responses; existing probes recover
without registry access. Probe ownership tolerates inherited image labels but requires the
attempt token, immutable image, exact name and recorded container ID. Image-declared
volumes are rejected to avoid orphaned anonymous volumes. Failed shutdown cannot authorize
removal or successful staging. The caller must hold the installation attempt lock.
This staging function is not yet connected to the replacement transaction. Latest focused
protocol and registration checks: 82 passed; real Docker execution remains unverified.

The source-retention primitive now durably records ownership before suppressing the
original container's restart policy, stopping with its configured bounded timeout and
renaming it. Exact container/image identities, launch-configuration hashes and stable
daemon settings are rechecked between mutations. Source credentials remain in Docker;
the journal stores identities, hashes, restart policy and nonsecret network MACs needed
when Docker clears addresses on stop. Recovery ignores changing storage-usage counters
and returns the immutable original image ID, never its mutable tag. Lost update/stop/rename
responses are replayable. Unproven shutdown cannot authorize backup or restoration.
Before re-reading the retained source, the controller must stop and remove its owned
candidate; this primitive does not exempt shared writers. Staging/source primitives import
successfully from the retained stdlib-only archive. Focused Docker/helper/registration
verification: 138 passed. Transaction wiring, daemon-bound readiness and real execution
acceptance remain outstanding.

Full regression after bundling these Docker components: 1055 passed, 10 deselected
in 77.35s. Lint and whitespace checks passed. This verifies existing regression
contracts and protocol fixtures, not live Docker or Windows acceptance.

Docker readiness now binds daemon ID, exact container/image IDs and start timestamp to
the one-use startup request. A bounded Docker exec checks the receipt process's Linux
boot/start identity and rejects zombie/dead states inside the private PID namespace;
the daemon's host PID is never interpreted locally. Container identity is checked again
after the probe. A controller can recover an existing durable owner record and reverify
the same live candidate. Crashes before owner-record publication still require transaction
cleanup/recreation. Shared PID namespaces, privileged namespace control and process-view
mount overlays defer rather than weakening ownership checks.

Docker retains startup environment across ordinary restarts. Once the fence is absent,
container-side startup may recognize its consumed request only under shared locks and
only when the request, terminal journal, readiness receipt, claim and current generation
all agree. The completed path uses the generation's durable activation receipt rather
than the latest attempt journal: later staging/deferred/failed attempts may replace that
journal without changing the installed generation. The engine records activation attempt,
token, outcome and states on both commit and restoration. Active fenced requests and
native startup behavior remain unchanged; a stale
generation is rejected. Focused Docker/native readiness/helper tests: 207 passed, with one
real unreaped-process Linux check skipped on macOS. Protocol fixtures are not live-daemon
acceptance. Subsequent transaction integration is described below.

The Docker installer adapter now implements the shared engine's staging, snapshot,
activation selection, migration and rollback-source selection interfaces. A durable Docker
job journals creation before mutation, validates exact daemon/container ownership, records
exit status before removal, and requires proven shutdown before restoration. Target and
restored migration jobs have distinct records; restoring the DB resets the stopped recovery
job so a previous successful receipt cannot skip validation of freshly restored data.
Lost create/delete responses, failed stops, changed recovery specifications and daemon
identity are covered by protocol fixtures.

The native installer's existing migration script is shared with Docker. Docker receives only
the mapped root mount, expected DB path, maintenance token and telemetry-disable settings;
it has no network or application credentials. Journal/fence/state/work validation precedes
launch. Real subprocess executions of that script against disposable SQLite fixtures preserve
existing user data for both migration and restoration. Focused Docker, helper, registration
and native installer checks: 180 passed. These fixtures do not prove actual Docker bind-mount
or daemon execution. Subsequent resident coordination and host setup are described below.

Docker resident coordination now uses the shared engine: retain the stopped source,
remove all owned candidate/migration writers before restoration, create an exact-image
replacement with live readiness, and publish its registration only after a durable terminal
decision. The engine persists whether post-terminal finalization is required; a missing or
failed finalizer leaves admission closed and never converts committed code/data into a
rollback. Restart policy and the source's original running/stopped state are restored during
idempotent finalization. Interrupted source-retention intent is resumed before recreation.
Historical retained sources must still match their stopped IDs, images, names and disabled
restart policies before another update is allowed.

Twenty full-engine Docker protocol cases cover commit, rollback, consecutive updates,
lost stop/rename/create/start replies, failed candidate stop, terminal finalization retry,
and changed retained-source ownership. SQLite fixtures verify restore ordering and retained
user data. The retained helper selects the Docker installer and resident controller from
the installation record. It resolves the official stable version tag through Docker's registry
API to an immutable manifest/index digest before staging. Pull explicitly selects the source
Linux architecture, including when it differs from the daemon host; the isolated probe
then verifies the actual image platform and installed version. Fenced recovery bypasses
registry discovery. Manual attempts remain available with automatic updates disabled.
These are simulated daemon tests, not live Docker acceptance. Retention coverage and remaining
limitations are described below; live platform acceptance remains required.

Retained Docker container cleanup now preserves the newest existing verified rollback
source and removes older owned, stopped, unmodified containers without force or volume
deletion. It runs outside maintenance under controller locks; a lost delete response is
reconciled before pruning the durable ownership record. Cleanup or diagnostic-storage
failure does not reinterpret a committed update. Shared daemon image cache remains externally managed; controller-owned container retention is bounded here.

Local attempt progress is mirrored into per-attempt retention receipts after authoritative
journal writes. Missing receipts preserve work and cannot invalidate a transaction. Cleanup
keeps current work, the newest completed rollback snapshot and two fully staged deferred or
failed attempts. Registered state, interpreter, environment, source and helper archive paths
are protected. It runs under attempt/registry locks outside maintenance, rejects mismatched
ownership and symlink receipts, checks mounted storage (including Linux bind mounts), and
uses symlink-resistant removal. Ten focused cases cover retained bounds and these exclusions.
Unknown work and crashed/unconfirmed staging remain preserved; automatic recovery/reclamation
of those cases is unfinished. Started cleanup has an external durable intent recording the
attempt UUID and directory device/inode. Retry revalidates ownership, mounted storage,
registered live paths and a surviving completed snapshot before continuing, even if the
inner receipt was already removed. Removal is synced before clearing the intent, including
when retry finds the target already absent. Nineteen retention cases cover this behavior.

Host setup uses `bubo update configure --container NAME_OR_ID --docker-socket /absolute/socket
--root /absolute/host/root`. It validates source provenance/layout and resident command,
then executes bounded private nonce/UID/shared-lock probes against the same running source
container. Compatible source code must lease initialized state even before registration;
setup publishes registration under the exclusive state lease, and provisional admissions
recheck registration and generation before proceeding. A stopped or incompatible source
defers setup. No user container is started to perform setup.

POSIX provisional state leases now lock the existing state directory, creating no lock file
for ordinary read-only admission. Docker setup requires admission capability version 2 and
uses the same directory lease. File admission and persistent identity share a nonblocking
OS locking primitive. Setup's exec probe verifies contention on the actual state-directory
inode as well as the temporary regular file; a shared-file/different-directory mapping fails.
The primitive uses POSIX flock or Win32 LockFileEx. Mocked Win32 tests cover handle width,
shared/exclusive flags, contention and identity persistence; they are not real Windows
acceptance. Windows scheduling, process-tree ownership/handoff, resident supervision and
other platform paths remain unfinished. POSIX pass_fds is not used on Windows.

The controller bundle and registration retain explicit host/container mappings, pinned
daemon identity and source-container analytics switches. Host privacy settings may further
restrict telemetry. Each Docker installation has a unique native scheduler identity derived
from its canonical path. Setup marks readiness only after scheduling succeeds; failed
scheduling is retryable and cannot run an update prematurely. Configuration does not change
`auto_update`. Setup failures produce sanitized local issue reports and consented outbox
records when the source state and installation identity can be established. Before source
state verification, reports are local-only under the host root's `.updates/setup-failures`.

Eighteen setup checks cover actual CLI dispatch, real subprocess lock probes, UID/nonce/
restart/exec ownership mismatches, timeout, custom state mapping, scheduler retry, registration
conflicts, opt-out preservation and failure telemetry identity/version. Scheduler commands
and Docker transport remain simulated in these checks; this does not certify a real daemon.

Reconfiguration after a committed update validates historical retained identities before
allowing their shared mounts through preflight. One additional integration check runs actual
preflight, real lock-probe subprocesses, native schedule generation (with a fake command
runner), and retained-helper update dispatch together. This caught an invalid setup-to-
preflight argument that isolated mocks had missed; the argument was removed.

Fresh service invocations and MCP reconnects can now reattach to an exact journaled
resident role during fenced recovery. They match launch arguments, cwd, transport kind
and state by digest, prove the former parent/child group absent, and acquire exclusive
activity leases before publishing replacement ownership. Credentials remain in memory.
The controller durably adopts ownership before sending commands addressed to the new
parent PID/start identity; an old mailbox request is never replayed. The fresh parent
stays childless until recovery instructs it to start. Missing child-spawn publication
still blocks recovery. Process-level reattachment is tested; end-to-end parent-loss
rollback completion and actionable restart diagnostics remain to be verified.

The retained-helper integration now tests parent death after code mutation, fresh
matching invocation, controller adoption, restored-generation readiness and fence
release. Original database rows and installation identity survive; credentials do
not appear in the journal or owner records. Proven-quiescent missing parents produce
`service_restart_required` (service/HTTP) or `mcp_reconnect_required` (stdio), including
when the attempt already has a durable terminal decision. These notices deduplicate
per attempt/code and resolved reports remove the obsolete restart action. An unproven
spawn or surviving child group still receives no claim that restarting is safe.

Native macOS scheduler acceptance is available as the explicit standalone fixture
`PYTHONPATH=src python tests/fixtures/updates/native_schedule_smoke.py`. It creates a
unique temporary launchd job running only a disposable helper, verifies start,
idempotent registration, changed-helper replacement, removal and both process exits.
The fixture passed against actual launchd. It never runs the updater or telemetry and
does not certify Linux/Windows schedulers or a real Docker daemon.

Windows implementation is in progress and automatic/manual mutation remains guarded.
Native platform primitives now use process-token SID ownership, `Scripts/python.exe`,
and creation-time process identity with fail-closed access/liveness checks. Task
Scheduler registration supports least-privilege direct Python execution, owned-task
refresh/removal and numeric task-state checks. Callers retain exclusive attempt
ownership while changing scheduling. These paths have mocked API/runner evidence,
not Windows acceptance. `InteractiveToken` requires a logged-in user and supports
closed-CLI updates with logon resumption. Pre-login/logged-out servicing is outside
the specified user-level scheduling contract. Job Object containment,
worker coordination and Windows replacement/durability validation remain required
before enabling updates. The standalone native scheduler fixture supports Windows
for future execution; only its macOS branch has been run on a real scheduler.

Windows installer subprocess containment now uses an atomically assigned, named
kill-on-close Job Object and an explicit inherited-stdio handle list. Journaled job
ownership is confirmed before child creation; recovery never terminates a collided
or unconfirmed job and refuses to recover another live helper's job. Global names
permit the same user's recovery across sessions. Completion requires zero active
job members, not merely root-process exit. Microsoft documents the destruction of
kill-on-close jobs after associated processes terminate and Job List support on
Windows 10 / Server 2016 onward. Mocked ABI/resource/crash-order checks pass. Native
Windows tests now exercise child/grandchild termination after coordinator death,
root-exit descendant detection, captured output, and read-only first-registration
lock contention; these are unexecuted on this macOS host. Windows mutation remains
disabled until worker/resident coordination and filesystem/platform acceptance pass.

Windows bounded-command staging now uses an isolated stdlib guardian inside the
Job Object, delegating launch semantics to subprocess and preserving combined
output, decoding, environment, cwd and timeout diagnostics. Six guardian tests
run real subprocesses on macOS; a native Windows batch-command comparison is
added but unexecuted. This backend is not dispatched by the public wrapper until
admission handoff and native validation are complete.

Windows cooperative service shutdown publishes complete stop tokens atomically
to generation-specific paths. Old stop callers and service watchers cannot erase
a newer generation request. Deterministic tests cover partial publication, stale
publishers, concurrent consumers and temporary cleanup on replacement failure.

Python and source staging now retain their subprocess ownership separately from
live-environment mutation ownership. Every staging subprocess publishes ownership
before writing; a subsequent command cannot overwrite unresolved ownership.
Retention may reclaim abandoned nonmutated staging attempts only under the
attempt lock after proving the recorded process group or Job is quiescent.
Missing, corrupt, ambiguous-starting and legacy ownership remain preserved.
The current attempt and registered rollback/recovery sources remain protected.
A process-level regression kills the staging coordinator, recovers its live
orphaned subprocess and verifies removal only after that subprocess stops.

POSIX installer and staging commands now use an isolated stdlib exec guardian.
A protocol/nonce launch intent and stable gate inode let recovery durably cancel
an unstarted command. The guardian validates the same inode and exact intent,
publishes its own session/PID/start identity durably under that gate, then execs
the requested command without changing process identity. A late guardian cannot
execute after cancellation, path replacement, or a different attempt's nonce.
Legacy unversioned starting records remain conservative safe deferrals. Tests
exercise both gate winners, publication failure, inode replacement, cancellation
write failure and guardian death before exec. Linux identity-format parity is
checked with controlled procfs input; native Linux acceptance remains required.

Docker daemon images are shared cache. Pre-pull absence cannot establish exclusive
controller ownership because concurrent clients may pull the same digest. Current
cleanup preserves that shared image cache; it removes only verified owned Docker
containers. Daemon-wide image-cache management remains external; no claim is made
that all Docker disk usage is bounded. Pre-pull absence cannot justify deletion.

Windows journal publication now flushes and closes the temporary file, then
requests same-directory MoveFileExW replacement with REPLACE_EXISTING and
WRITE_THROUGH only. Cross-directory moves and copy/delete/reboot fallbacks are
excluded. Publication errors may occur after replacement: recovery must inspect
the destination rather than assuming the old record survived. This primitive
is not a general directory durability guarantee, including newly created parent
directories. Windows sync_directory remains explicitly unsupported, and Windows
updates remain disabled pending transaction durability and coordination work.
Native Unicode-path and sharing-violation acceptance tests are added but have not
run on this macOS host.

Windows bounded execution now optionally records one unique Job per launch in a
credential-free installation ledger. Its creation callback commits ownership
before any child can start. Exclusive admission drains only after all installation
and state leases are acquired: pending live owners defer, unknown ownership blocks, and
confirmed dead/reused owners require whole-Job recovery before record removal.
Successful closure proves zero members before retiring its receipt; failed closure
keeps evidence. Name collisions are never treated as owned jobs. Atomic-publication
temporaries have explicit non-authoritative names and cannot be mistaken for Job
receipts after a crash. Empty-ledger draining performs no filesystem writes.
The public subprocess path is not yet switched: detached-worker acknowledgment,
first-registration proof, resident coordination, native acceptance and Windows
transaction durability remain required before enabling native updates.

A failed Windows Job close now durably abandons that launch while retaining its
ownership evidence. Abandonment permanently revokes creation rights; after
exclusive admission, recovery may stop that Job even if its original MCP/service
parent remains alive. An uncreated/colliding name still cannot authorize Job
termination. Failed abandonment publication leaves evidence and explicit restart
guidance. An uncertain creation callback may be reconciled only when the on-disk
record is exactly this object's attempted job_created=false-to-true transition;
other ownership changes remain blocked. Tests cover publication errors before
and after replacement and recovery with a surviving caller. Native Windows Job
acceptance for this path is added but remains unexecuted on this host.

Detached-worker handle primitives now verify that the guardian is a member of
the named Job, kill-on-close is set, and neither breakaway flag is set before
retaining a non-inheritable handle. The parent relinquishes its handles only
after an acknowledgement validator succeeds. Once its Job handle is released,
process-handle cleanup failure cannot turn committed handoff into a failed launch.
Guardian termination uses native ExitProcess to retain admission through exit.
Durable transfer now changes only the exact parent receipt's owner PID/start
identity. Parent validation uses its retained child process handle even after
child exit. A fast-finishing guardian retains its quiescent receipt until the
parent commits handle handoff and publishes a matching acknowledgement; either
side may then clean up. Uncertain acknowledgement cleanup never fails a committed
launch. Exclusive recovery handles abandoned terminal records and orphan markers.
Retirement verifies exact ownership and sole Job membership, then exits natively
even if publication fails. The retained stdlib-only guardian acquires existing
environment/installation/state admission locks, checks registration, generation
and maintenance, adopts ownership, runs the worker and waits for every descendant.
It remains single-threaded and never unwinds admission into application cleanup.
The parent launcher now holds its own admission through handoff or cancellation.
It invokes the retained guardian with `-I -S`, and accepts success only after
durable `worker_started` acknowledgement following successful worker process
creation. Startup errors use bounded codes without paths or exception text.
Failed transfer stops the entire Job before reconciling exact parent/guardian
receipts; failed stop leaves the journal untouched and reports unresolved cleanup.
Bookkeeping after committed handle transfer cannot turn success into a retryable
launch failure. Registered Windows poller workers use this launcher and retain
their configured arguments and output log. Windows bootstrap remains disabled.
Crash-boundary native acceptance remains required. Native handle survival,
guardian death and retained-guardian admission coverage are added but unexecuted;
no Windows enablement follows from mocked API tests.

The public bounded-command wrapper now uses the Windows Job backend for native
registered installations while retaining activity through command completion and
whole-Job cleanup. Combined output, exit status, environment/cwd, partial timeout
transcripts and actionable command-not-found diagnostics remain covered. Receipt
or admission storage failures are not relabeled as missing commands. Registration
is revalidated after acquiring installation and state locks, then checked again
against the wrapper's selected installation before publishing Job ownership.
Unregistered Windows dispatch remains unchanged until first-registration safety
and native acceptance are established. Native public-wrapper batch-command
coverage is present but unexecuted on this host.

Windows resident supervisors broker detached worker launches from outside their
non-breakaway resident Job. The bounded loopback protocol authenticates a
transient token, validates the source process creation identity and exact Job
membership, and duplicates only its live output handle. Launch configuration stays
in memory; broker credentials are stripped from worker environments. Guardian
admission and startup acknowledgment remain the commit boundary. Transport loss
or uncertain guardian cleanup reports an unknown outcome; the transport never
automatically resubmits the launch; a committed worker survives a lost response. Broker shutdown
must complete before resident Job teardown. Native survival/admission acceptance
is required before enabling Windows resident updates.

Queued-TTL retries reconcile a stable hashed review identity under a launch
mutex held through guardian handoff. A live started guardian is reused; uncertain
ownership defers. A dead owner's receipt must remain unchanged, and any confirmed
Job must be empty before a replacement launch is allowed. This retry path never
terminates a Job that may contain an adopting guardian. Job payload publication
is atomic so a delayed worker sees a complete payload during a retry. Native
Windows acceptance of these invariants remains required before enablement.

Windows service shutdown first publishes the existing service-generation stop
token after verifying the supervised child's PID and native creation identity.
The supervisor then retires its worker broker and allows a bounded cooperative
exit before whole-Job termination/proof. A stale PID or generation never authorizes
a stop request, and failed whole-Job cleanup never publishes a stopped state.

Supervised Windows HTTP uses a private stdin pipe for cooperative shutdown while
preserving its normal stdout/stderr. Closing the supervisor's writer requests
normal Uvicorn shutdown; readiness-fenced startup obeys the same signal. The
control marker is consumed before application work and stripped from detached
worker environments. The reader uses raw descriptor I/O so independent server
shutdown cannot leave a daemon holding Python's buffered-stdin lock. Client stdio
MCP input is not used for this HTTP control channel.
