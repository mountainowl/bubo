# Data Model — F3

UpdateConfig: auto_update boolean default true. Parse through existing TOML config loader.
InstallationDescriptor: canonical executable/environment, manager/type, official release channel,
registered state roots, helper identity, owned schedule, runtime platform/architecture; sensitive launch
settings stay local. All roots sharing mutable code are locked in canonical order or update defers.
ReleaseCandidate: source version, target stable version, platform/Python constraints, artifact digest,
verified official origin. Immutable within an attempt.
ActivityLease: installation, state root, process-start identity (not PID alone), operation, generation;
OS-backed shared lifetime lock with persisted generation fence. Child handoff overlaps parent lease.
AttemptJournal v1: UUID, trigger manual/automatic, initiating state root (local only), phase/sequence, timestamps, producer/from/target versions, identity snapshot,
artifact/backup references, last healthy generation, protocol version and recovery outcome.
RecoverySnapshot: consistent SQLite backup plus integrity result, config digest/copy and exact identity
snapshot. Report/outbox/journal/helper live outside this rollback snapshot. Never restore their old copies.

States: discovered -> staged -> idle_try -> exclusive -> backed_up -> installing -> migrating ->
validating -> committed -> reopened. Any pre-mutation problem -> deferred/failed with normal operation.
Any post-mutation problem -> recovering -> restored/reopened OR blocked (admission remains closed).
Journal before each transition; idempotent restart examines last intent and actual state, never assumes
an absent completion marker means mutation did not occur. Duplicate wakeups join existing attempt.
Terminal committed/restored state is durable before releasing the identity guard and admission fence.

Busy idle_try releases all partial locks immediately and stays staged/deferred without a maintenance fence.
