"""One journaled transaction for manual and unattended updates.

Installation adapters own package replacement; this module owns admission, SQLite
snapshots, identity, and recovery. No new work is admitted before durable commit.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bubo.analytics_config import AnalyticsConfig
from bubo.update_admission import UpdateBusy, exclusive
from bubo.update_identity import (
    atomic_json,
    guard_path,
    guarded_identity,
    protect_identity,
    release_identity,
    sync_directory,
)
from bubo.update_reporting import FailureStore


class ResidentRestartRequired(OSError):
    """A proven stopped resident needs a fresh invocation's credentials."""

    def __init__(self, kind: str):
        if kind not in {"service", "mcp-http", "mcp-stdio"}:
            raise ValueError("Unknown resident kind")
        self.code = "mcp_reconnect_required" if kind == "mcp-stdio" else "service_restart_required"
        super().__init__(self.code)


@dataclass(frozen=True)
class Release:
    version: str
    artifact: str
    sha256: str


class Installer(Protocol):
    kind: str

    def stage(self, release: Release, destination: Path) -> None: ...
    def snapshot(self, destination: Path) -> None: ...
    def activate(self, destination: Path) -> None: ...
    def restore(self, destination: Path) -> None: ...
    def migrate_and_check(self, states: list[Path], token: str) -> None: ...
    def writers_quiescent(self) -> bool: ...
    def generation_version(self, version: str) -> str: ...
    def maintenance(self): ...


class ResidentController(Protocol):
    """Own resident quiescence, launch, and process receipts across a transaction."""

    def quiesce(self, journal: dict) -> None: ...
    def stop(self, journal: dict) -> bool: ...
    def start(self, journal: dict, *, outcome: str) -> None: ...
    def verify(self, journal: dict, *, outcome: str) -> bool: ...


@dataclass(frozen=True)
class UpdateResult:
    status: str
    attempt_id: str | None = None
    code: str | None = None

    @property
    def exit_code(self) -> int:
        if self.status in {"updated", "current", "restored"}:
            return 0
        return 75 if self.status == "deferred" else 1


def _backup_db(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(destination)) as backup,
    ):
        original.backup(backup)
        if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise OSError("Database backup failed integrity validation")
    with destination.open("r+b" if sys.platform == "win32" else "rb") as stream:
        os.fsync(stream.fileno())
    return True


def _restore_db(backup: Path | None, destination: Path) -> None:
    # The exclusive lease and proven writer quiescence are required by callers.
    for suffix in ("-wal", "-shm"):
        Path(str(destination) + suffix).unlink(missing_ok=True)
    if backup is None:
        destination.unlink(missing_ok=True)
    else:
        temporary = destination.with_suffix(".restore")
        shutil.copy2(backup, temporary)
        with temporary.open("r+b" if sys.platform == "win32" else "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    sync_directory(destination.parent)


class UpdateEngine:
    def __init__(
        self,
        installation: Path,
        states: list[Path],
        installer: Installer,
        *,
        source_version: str,
        analytics: AnalyticsConfig,
        enabled,
        producer_version: str,
        consent=None,
        reporting_policy=None,
        residents: ResidentController | None = None,
    ):
        self.installation = installation.resolve()
        self.states = sorted({state.resolve() for state in states})
        if not self.states:
            raise ValueError("An update requires registered state")
        self.installer = installer
        self.source_version = source_version
        self.producer_version = producer_version
        self.analytics = analytics
        self.consent = consent
        self.reporting_policy = reporting_policy
        self.enabled = enabled
        self.residents = residents
        self.journal_path = self.installation / "attempt.json"
        self.fence = self.installation / "maintenance.json"

    def _write(self, journal: dict, phase: str) -> None:
        journal["phase"] = phase
        journal["sequence"] += 1
        atomic_json(self.journal_path, journal)
        try:
            from bubo.update_retention import remember

            remember(journal)
        except OSError, ValueError, KeyError:
            # Retention is optional; a missing receipt preserves the snapshot.
            pass

    def _record(self, journal: dict, code: str, *, recovery: str) -> None:
        try:
            self._persist_failure(journal, code, recovery=recovery)
        except Exception:
            print(
                "Bubo update diagnostic storage unavailable; recovery continues safely.",
                file=sys.stderr,
            )

    def _persist_failure(self, journal: dict, code: str, *, recovery: str) -> None:
        restart_required = code in {"mcp_reconnect_required", "service_restart_required"}
        failure_id = uuid.uuid5(
            uuid.UUID(journal["attempt_id"]),
            code if restart_required else f"{journal['phase']}:{journal['sequence']}:{code}",
        )
        state = Path(journal["initiating_state"])
        cfg, consent = (
            self.reporting_policy(state)
            if self.reporting_policy is not None
            else (self.analytics, None if self.consent is None else self.consent() is True)
        )
        FailureStore(state).record(
            attempt_id=journal["attempt_id"],
            failure_id=str(failure_id),
            phase=journal["phase"],
            error_code=code,
            cfg=cfg,
            install_type=self.installer.kind,
            from_version=journal["source"],
            target_version=journal["target"],
            recovery_status=recovery,
            producer_version=self.producer_version,
            consent=consent,
        )

    def _allowed(self, trigger: str, initiating_state: Path) -> bool:
        return all(
            not (state / "update-registration-conflict.json").exists()
            and ((trigger == "manual" and state == initiating_state) or self.enabled(state))
            for state in self.states
        )

    def run(
        self, release: Release, *, trigger: str = "automatic", initiating_state: Path | None = None
    ) -> UpdateResult:
        if trigger not in {"automatic", "manual"}:
            raise ValueError("Invalid update trigger")
        initiating_state = (initiating_state or self.states[0]).resolve()
        if initiating_state not in self.states:
            raise ValueError("Unregistered initiating state")
        if self.fence.exists():
            return UpdateResult("deferred", code="recovery_required")
        if not self._allowed(trigger, initiating_state):
            return UpdateResult("deferred", code="disabled")
        # Serialize stage/journal ownership as well as mutation. A staging process
        # does not fence activity; ordinary operations never acquire this lock.
        from bubo.update_admission import _file_lock

        try:
            with _file_lock(self.installation / "attempt.lock", exclusive=True):
                # Recheck only under the attempt lock: another updater may have
                # died after our optimistic check, leaving recovery intent.
                if self.fence.exists():
                    return UpdateResult("deferred", code="recovery_required")
                generation = self.installation / "generation.json"
                if generation.exists():
                    current = json.loads(generation.read_text())["version"]
                    if current == release.version:
                        return UpdateResult("current")
                    if current != self.source_version:
                        return UpdateResult("deferred", code="generation_changed")
                if release.version == self.source_version:
                    return UpdateResult("current")
                previous = None
                if self.journal_path.exists():
                    previous = json.loads(self.journal_path.read_text())
                    if previous.get("phase") not in {
                        "committed",
                        "restored",
                        "deferred",
                        "failed",
                        "staging",
                    }:
                        return UpdateResult("deferred", code="recovery_required")
                reusable = (
                    previous
                    if (
                        previous
                        and previous.get("phase") == "deferred"
                        and previous.get("staged")
                        and previous.get("source") == self.source_version
                        and previous.get("target") == release.version
                        and previous.get("sha256") == release.sha256
                    )
                    else None
                )
                return self._attempt(release, trigger, initiating_state, reusable)
        except UpdateBusy:
            return UpdateResult("deferred", code="busy")

    def _attempt(
        self, release: Release, trigger: str, initiating_state: Path, previous: dict | None = None
    ) -> UpdateResult:
        attempt = previous["attempt_id"] if previous else str(uuid.uuid4())
        work = self.installation / "attempts" / attempt
        work.mkdir(parents=True, mode=0o700, exist_ok=previous is not None)
        journal = {
            "protocol_version": 1,
            "attempt_id": attempt,
            "trigger": trigger,
            "initiating_state": str(initiating_state),
            "source": self.source_version,
            "target": release.version,
            "phase": "staging",
            "sequence": 0,
            "states": [str(state) for state in self.states],
            "backups": [],
            "mutated": False,
            "work": str(work),
            "token": uuid.uuid4().hex,
            "sha256": release.sha256,
            "staged": False,
            "residents_required": self.residents is not None,
            "resident_finalization_required": callable(getattr(self.residents, "finalize", None)),
        }
        if previous:
            journal = {**previous, "trigger": trigger, "initiating_state": str(initiating_state)}
        journal["residents_required"] = bool(
            journal.get("residents_required") or self.residents is not None
        )
        journal["resident_finalization_required"] = bool(
            journal.get("resident_finalization_required")
            or callable(getattr(self.residents, "finalize", None))
        )
        self._write(journal, "staging")
        try:
            FailureStore(initiating_state).ensure_capacity()
            if not journal["staged"]:
                self.installer.stage(release, work)
                journal["staged"] = True
                self._write(journal, "staging")
            with self.installer.maintenance() as bridge, exclusive(self.installation, self.states):
                registration = self.installation / "installation.json"
                if registration.exists():
                    registered = sorted(json.loads(registration.read_text())["states"])
                    if registered != [str(state) for state in self.states]:
                        self._write(journal, "deferred")
                        return UpdateResult("deferred", attempt, "inventory_changed")
                if not self._allowed(trigger, initiating_state):
                    self._write(journal, "deferred")
                    return UpdateResult("deferred", attempt, "disabled")
                if journal.get("residents_required") and self.residents is None:
                    self._write(journal, "deferred")
                    return UpdateResult("deferred", attempt, "resident_controller_unavailable")
                atomic_json(self.fence, {"attempt_id": attempt, "token": journal["token"]})
                self._release_bridge(bridge)
                try:
                    return self._mutate(journal, work)
                except Exception:
                    return self._failed(journal, work)
        except UpdateBusy:
            terminal = self._terminal_failure(journal)
            if terminal is not None:
                return terminal
            self._write(journal, "deferred")
            return UpdateResult("deferred", attempt, "busy")
        except Exception:
            terminal = self._terminal_failure(journal)
            if terminal is not None:
                return terminal
            self._record(journal, "unexpected_error", recovery="not_needed")
            self._write(journal, "failed")
            return UpdateResult("failed", attempt, "preflight_failed")

    def _mutate(self, journal: dict, work: Path) -> UpdateResult:
        self._require_resident_controller(journal)
        if journal.get("residents_required"):
            self._write(journal, "quiescing")
            self.residents.quiesce(journal)
        self._write(journal, "backup")
        for index, state in enumerate(self.states):
            protect_identity(state)
            backup = work / f"database-{index}.sqlite"
            present = _backup_db(state / "reviewer.sqlite", backup)
            journal["backups"].append(str(backup) if present else None)
            self._write(journal, "backup")
        self.installer.snapshot(work)
        # After this intent, recovery must assume the package may have changed,
        # even if interruption prevents publication of the completion marker.
        journal["mutated"] = True
        self._write(journal, "install")
        self.installer.activate(work)
        self._write(journal, "migration")
        self.installer.migrate_and_check(self.states, journal["token"])
        for state in self.states:
            guarded_identity(state)
        if journal.get("residents_required"):
            self._write(journal, "readiness")
            self._start_residents(journal, outcome="committed")
        atomic_json(
            self.installation / "generation.json",
            {
                "version": self.installer.generation_version(journal["target"]),
                "activation": {
                    "attempt_id": journal["attempt_id"],
                    "token": journal["token"],
                    "outcome": "committed",
                    "states": journal["states"],
                },
            },
        )
        self._write(journal, "committed")
        self._reopen()
        return UpdateResult("updated", journal["attempt_id"])

    def _failed(self, journal: dict, work: Path) -> UpdateResult:
        terminal = self._terminal_failure(journal)
        if terminal is not None:
            return terminal
        code = "restart_failed" if journal["phase"] == "readiness" else "unexpected_error"
        self._record(journal, code, recovery="pending")
        return self._recover(journal, work)

    def _terminal_failure(self, journal: dict) -> UpdateResult | None:
        # A context-manager exit or a write/fsync error may occur after a
        # terminal record was published. Never turn that decision into rollback.
        try:
            persisted = json.loads(self.journal_path.read_text())
            if (
                persisted.get("attempt_id") == journal["attempt_id"]
                and persisted.get("token") == journal["token"]
                and persisted.get("phase") in {"committed", "restored"}
            ):
                journal.update(persisted)
        except OSError, ValueError:
            pass
        if journal["phase"] in {"committed", "restored"}:
            self._record(journal, "unexpected_error", recovery="blocked")
            return UpdateResult("failed", journal["attempt_id"], "finalization_failed")
        return None

    def _recover(self, journal: dict, work: Path) -> UpdateResult:
        self._write(journal, "recovery")
        try:
            self._require_resident_controller(journal)
            if journal.get("residents_required") and not self.residents.stop(journal):
                raise OSError("Resident quiescence is unproven")
            if not self.installer.writers_quiescent():
                raise OSError("Mutation writer is not proven quiescent")
            if journal["mutated"]:
                self.installer.restore(work)
                for state, backup in zip(self.states, journal["backups"], strict=True):
                    _restore_db(Path(backup) if backup else None, state / "reviewer.sqlite")
                self.installer.migrate_and_check(self.states, journal["token"])
            for state in self.states:
                if guard_path(state).exists():
                    guarded_identity(state)
            if journal.get("residents_required"):
                self._start_residents(journal, outcome="restored")
            atomic_json(
                self.installation / "generation.json",
                {
                    "version": self.installer.generation_version(journal["source"]),
                    "activation": {
                        "attempt_id": journal["attempt_id"],
                        "token": journal["token"],
                        "outcome": "restored",
                        "states": journal["states"],
                    },
                },
            )
            self._write(journal, "restored")
            self._reopen()
            self._finish_recovery(journal)
            return UpdateResult("failed", journal["attempt_id"], "restored")
        except Exception as exc:
            code = exc.code if isinstance(exc, ResidentRestartRequired) else "unexpected_error"
            self._record(journal, code, recovery="blocked")
            if journal["phase"] in {"committed", "restored"}:
                return UpdateResult("failed", journal["attempt_id"], "finalization_failed")
            self._write(journal, "blocked")
            return UpdateResult(
                "failed",
                journal["attempt_id"],
                code if isinstance(exc, ResidentRestartRequired) else "recovery_blocked",
            )

    def _require_resident_controller(self, journal: dict) -> None:
        if journal.get("residents_required") and self.residents is None:
            raise OSError("Resident recovery controller is unavailable")
        if journal.get("resident_finalization_required") and not callable(
            getattr(self.residents, "finalize", None)
        ):
            raise OSError("Resident finalization controller is unavailable")

    def _finish_recovery(self, journal: dict) -> None:
        try:
            FailureStore(Path(journal["initiating_state"])).finish_recovery(
                journal["attempt_id"], journal["phase"]
            )
        except Exception:
            print("Bubo recovery completed; local diagnostic refresh unavailable.", file=sys.stderr)

    def _start_residents(self, journal: dict, *, outcome: str) -> None:
        self._require_resident_controller(journal)
        self.residents.start(journal, outcome=outcome)
        if not self.residents.verify(journal, outcome=outcome):
            raise OSError("Replacement resident readiness is unproven")

    def _release_bridge(self, bridge) -> None:
        if sys.platform != "win32" or bridge is None:
            return
        from bubo.update_helper import installation_for_environment

        record = json.loads((self.installation / "installation.json").read_text())
        environment = Path(record["environment"])
        if installation_for_environment(environment).resolve() != self.installation:
            raise OSError("Registered installation admission cannot replace the environment bridge")
        if not self.fence.exists():
            raise OSError("Environment bridge requires durable maintenance intent")
        # Caller holds exclusive installation/state admission. New registered
        # callers use those locks, so closing our bridge cannot admit new work.
        bridge.close()

    def _reopen(self) -> None:
        journal = json.loads(self.journal_path.read_text())
        if journal.get("resident_finalization_required"):
            self._require_resident_controller(journal)
            if journal["phase"] not in {"committed", "restored"}:
                raise OSError("Resident finalization requires a terminal decision")
            self.residents.finalize(journal, outcome=journal["phase"])
        for state in self.states:
            if guard_path(state).exists():
                release_identity(state)
        self.fence.unlink(missing_ok=True)
        sync_directory(self.installation)

    def recover(self) -> UpdateResult:
        """Retained helper entry after process death; never guesses missing intent."""
        from bubo.update_admission import _file_lock

        try:
            with (
                _file_lock(self.installation / "attempt.lock", exclusive=True),
                self.installer.maintenance() as bridge,
                exclusive(self.installation, self.states),
            ):
                if not self.fence.exists():
                    return UpdateResult("current")
                self._release_bridge(bridge)
                journal = json.loads(self.journal_path.read_text())
                if journal["protocol_version"] != 1:
                    return UpdateResult("failed", code="unsupported_protocol")
                if journal["states"] != [str(state) for state in self.states]:
                    return UpdateResult("failed", code="state_mismatch")
                if journal["phase"] in {"committed", "restored"}:
                    if journal.get("residents_required"):
                        try:
                            self._require_resident_controller(journal)
                            if not self.residents.stop(journal):
                                raise OSError("Resident quiescence is unproven")
                            self._start_residents(journal, outcome=journal["phase"])
                        except Exception as exc:
                            # The durable terminal decision must not become a
                            # rollback instruction merely because restart failed.
                            code = (
                                exc.code
                                if isinstance(exc, ResidentRestartRequired)
                                else "resident_recovery_blocked"
                            )
                            self._record(
                                journal,
                                code
                                if isinstance(exc, ResidentRestartRequired)
                                else "unexpected_error",
                                recovery="blocked",
                            )
                            return UpdateResult("failed", journal["attempt_id"], code)
                    try:
                        self._reopen()
                    except Exception:
                        self._record(journal, "unexpected_error", recovery="blocked")
                        return UpdateResult("failed", journal["attempt_id"], "finalization_failed")
                    self._finish_recovery(journal)
                    return UpdateResult(
                        "updated" if journal["phase"] == "committed" else "restored",
                        journal["attempt_id"],
                    )
                self._record(journal, "interrupted", recovery="pending")
                return self._recover(journal, Path(journal["work"]))
        except UpdateBusy:
            return UpdateResult("deferred", code="busy")
