"""One-use startup acknowledgement from a fenced replacement resident.

The retained supervisor owns launching, live-process validation and rollback.
This gate never grants maintenance admission to application work.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from bubo.update_admission import UpdateBusy, _file_lock
from bubo.update_identity import atomic_json, sync_directory
from bubo.update_installers import _start_identity
from bubo.update_registration import journal_path, load_registration
from bubo.version import installed_version

REQUEST_ENV = "BUBO_UPDATE_START_REQUEST"


class StartupRejected(RuntimeError):
    """A candidate must exit without serving application work."""


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except OSError, ValueError:
        raise StartupRejected("Resident startup state is unavailable") from None
    if not isinstance(value, dict):
        raise StartupRejected("Invalid resident startup state")
    return value


def _release(version: str) -> str:
    return version.split("+", 1)[0]


def _registration(state: Path) -> dict:
    try:
        return load_registration(state)
    except OSError, ValueError, TypeError:
        raise StartupRejected("Resident startup registration is unavailable") from None


def _mapped_path(state: Path, value) -> Path:
    try:
        return journal_path(state, value)
    except OSError, ValueError, TypeError:
        raise StartupRejected("Resident startup path mapping is invalid") from None


class ResidentReadiness:
    """Controller receipt for one launched child, not ownership inferred from a file.

    The caller retains its Popen handle and owns termination/reaping on failure.
    Environment credentials never enter either the request or ownership journal.
    """

    def __init__(self, state: Path, kind: str, *, outcome: str = "committed"):
        if kind not in {"service", "mcp-http", "mcp-stdio"} or outcome not in {
            "committed",
            "restored",
        }:
            raise StartupRejected("Unsupported resident startup")
        self.state = state.resolve()
        registration = _registration(self.state)
        location = registration.get("installation")
        if not isinstance(location, str) or not Path(location).is_absolute():
            raise StartupRejected("Invalid resident controller path")
        self.installation = Path(location)
        journal = _read(self.installation / "attempt.json")
        version = journal.get("target" if outcome == "committed" else "source")
        if not isinstance(version, str) or not version:
            raise StartupRejected("Requested resident version is unavailable")
        self.request_id = str(uuid.uuid4())
        self.path = self.installation / "residents" / f"{self.request_id}.json"
        self.ready_path = self.path.with_suffix(".ready.json")
        self.owner_path = self.path.with_suffix(".owner.json")
        self.request = {
            "protocol_version": 1,
            "attempt_id": journal.get("attempt_id"),
            "token": journal.get("token"),
            "state": str(self.state),
            "kind": kind,
            "version": version,
            "outcome": outcome,
        }
        self.process: subprocess.Popen | None = None
        self.identity: str | None = None
        self._validate_attempt()
        atomic_json(self.path, self.request)

    def _validate_attempt(self) -> None:
        journal = _read(self.installation / "attempt.json")
        fence = _read(self.installation / "maintenance.json")
        permitted = (
            {"migration", "readiness", "committed"}
            if self.request["outcome"] == "committed"
            else {"recovery", "restored"}
        )
        if (
            not self.request["token"]
            or not self.request["attempt_id"]
            or journal.get("phase") not in permitted
            or str(self.state) not in journal.get("states", [])
            or journal.get("target" if self.request["outcome"] == "committed" else "source")
            != self.request["version"]
            or any(
                record.get(key) != self.request[key]
                for record in (journal, fence)
                for key in ("attempt_id", "token")
            )
        ):
            raise StartupRejected("Resident controller attempt changed")

    def environment(self, inherited: dict[str, str]) -> dict[str, str]:
        env = dict(inherited)
        # Candidates must never inherit the migration subprocess's fence bypass.
        env.pop("BUBO_MAINTENANCE_TOKEN", None)
        env[REQUEST_ENV] = self.request_id
        return env

    def bind(self, process: subprocess.Popen) -> None:
        """Bind to the supervisor's actual child immediately after Popen."""
        if self.process is not None:
            raise StartupRejected("Resident child is already bound")
        self.process = process
        self._validate_attempt()
        self.identity = _start_identity(process.pid)
        if process.poll() is not None or not self.identity:
            raise StartupRejected("Resident child exited before ownership was recorded")
        atomic_json(
            self.owner_path,
            {**self.request, "pid": process.pid, "process_identity": self.identity},
        )

    def _owns_receipt(self, receipt: dict) -> bool:
        if sys.platform == "win32":
            owns_member = getattr(self.process, "owns_member", None)
            return bool(
                owns_member and owns_member(receipt.get("pid"), receipt.get("process_identity"))
            )
        return (
            receipt.get("pid") == self.process.pid
            and receipt.get("process_identity") == self.identity
        )

    def verify(self) -> bool:
        """Recheck immediately before commit; a prior success is not cached."""
        self._validate_attempt()
        if self.process is None or not self.identity:
            raise StartupRejected("Resident child ownership is unavailable")
        if (
            self.process.poll() is not None
            or _start_identity(self.process.pid) != self.identity
            or _read(self.path) != self.request
            or _read(self.owner_path)
            != {**self.request, "pid": self.process.pid, "process_identity": self.identity}
        ):
            raise StartupRejected("Resident child is no longer the launched process")
        if not self.ready_path.exists():
            return False
        receipt = _read(self.ready_path)
        loaded = receipt.get("loaded_version")
        if (
            any(receipt.get(key) != value for key, value in self.request.items())
            or not self._owns_receipt(receipt)
            or not isinstance(loaded, str)
            or _release(loaded) != _release(self.request["version"])
            or not self.path.with_suffix(".claim").is_file()
            or self.process.poll() is not None
        ):
            raise StartupRejected("Resident readiness does not match the live child")
        return True

    def wait(self, *, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while not self.verify():
            if time.monotonic() >= deadline:
                raise StartupRejected("Resident readiness timed out")
            time.sleep(0.05)


class ResidentStartup:
    def __init__(
        self, state: Path, kind: str, request_id: str, *, allow_completed_docker: bool = False
    ):
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError
        except ValueError, AttributeError:
            raise StartupRejected("Invalid resident startup request") from None
        self.state = state.resolve()
        registration = _registration(self.state)
        location = registration.get("installation")
        if not isinstance(location, str):
            raise StartupRejected("Invalid resident controller path")
        self.installation = Path(location)
        if not self.installation.is_absolute():
            raise StartupRejected("Invalid resident controller path")
        self.path = self.installation / "residents" / f"{request_id}.json"
        self.request = _read(self.path)
        self.version = installed_version()
        if not isinstance(self.version, str) or not self.version:
            raise StartupRejected("Resident installed version is unavailable")
        if (
            self.request.get("protocol_version") != 1
            or self.request.get("kind") != kind
            or _mapped_path(self.state, self.request.get("state")) != self.state
            or not isinstance(self.request.get("version"), str)
            or _release(self.request["version"]) != _release(self.version)
            or self.request.get("outcome") not in {"committed", "restored"}
        ):
            raise StartupRejected("Resident does not match requested startup")
        self.ready_path = self.path.with_suffix(".ready.json")
        self.claim = self.path.with_suffix(".claim")
        self.signaled = False
        self.completed_docker = False
        if (
            allow_completed_docker
            and registration.get("external") == "docker"
            and registration.get("view") in {"container", "shared"}
            and not (self.installation / "maintenance.json").exists()
        ):
            # Docker retains its creation environment on ordinary restarts. A
            # consumed request is bypassed only for its proven terminal generation.
            with (
                _file_lock(self.installation / "activity.lock", exclusive=False),
                _file_lock(self.state / "update-activity.lock", exclusive=False),
            ):
                if not (self.installation / "maintenance.json").exists():
                    self._validate_docker_activation()
                    self.completed_docker = True
                    return
        self._validate_fence()

    def _validate_docker_activation(self) -> None:
        """Caller holds shared admission and has proved the fence is absent."""
        generation = _read(self.installation / "generation.json")
        activation = generation.get("activation", {})
        receipt = _read(self.ready_path)
        if (
            _read(self.path) != self.request
            or not self.request.get("token")
            or not self.request.get("attempt_id")
            or any(
                activation.get(key) != self.request[key]
                for key in ("attempt_id", "token", "outcome")
            )
            or self.state
            not in [_mapped_path(self.state, path) for path in activation.get("states", [])]
            or _release(str(generation.get("version"))) != _release(self.version)
            or not self.claim.is_file()
            or any(receipt.get(key) != value for key, value in self.request.items())
            or _release(str(receipt.get("loaded_version"))) != _release(self.version)
        ):
            raise StartupRejected("Docker startup generation was not committed")

    @classmethod
    def from_environment(cls, kind: str, state: Path | None = None) -> ResidentStartup | None:
        request_id = os.environ.pop(REQUEST_ENV, None)
        if request_id is None:
            return None
        if state is None:
            from bubo import paths

            state = paths.DB.parent
        gate = cls(state, kind, request_id, allow_completed_docker=True)
        return None if gate.completed_docker else gate

    def _journal(self) -> dict:
        journal = _read(self.installation / "attempt.json")
        if (
            journal.get("attempt_id") != self.request.get("attempt_id")
            or journal.get("token") != self.request.get("token")
            or not journal.get("token")
            or self.state
            not in [_mapped_path(self.state, path) for path in journal.get("states", [])]
            or _read(self.path) != self.request
            or _release(
                str(journal.get("target" if self.request["outcome"] == "committed" else "source"))
            )
            != _release(self.request["version"])
        ):
            raise StartupRejected("Resident startup attempt changed")
        return journal

    def _validate_fence(self) -> None:
        self._journal()
        fence = _read(self.installation / "maintenance.json")
        if fence.get("attempt_id") != self.request.get("attempt_id") or fence.get(
            "token"
        ) != self.request.get("token"):
            raise StartupRejected("Resident startup fence changed")
        phase = self._journal().get("phase")
        permitted = (
            {"migration", "readiness", "committed"}
            if self.request["outcome"] == "committed"
            else {"recovery", "restored"}
        )
        if phase not in permitted:
            raise StartupRejected("Resident startup phase changed")

    def signal_ready(self) -> None:
        """Call only after the real resident startup resources are acquired."""
        self._validate_fence()
        identity = _start_identity(os.getpid())
        if not identity:
            raise StartupRejected("Resident process identity is unavailable")
        try:
            # Claims remain after crashes; a replacement needs a fresh request.
            if sys.platform == "win32":
                from bubo.update_windows_files import claim_file

                claim_file(self.claim)
            else:
                fd = os.open(self.claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
                sync_directory(self.claim.parent)
        except FileExistsError:
            raise StartupRejected("Resident startup request was already consumed") from None
        self._validate_fence()
        atomic_json(
            self.ready_path,
            {
                **self.request,
                "pid": os.getpid(),
                "process_identity": identity,
                "loaded_version": self.version,
            },
        )
        self.signaled = True

    def committed(self) -> bool:
        if not self.signaled:
            raise StartupRejected("Resident readiness has not been acknowledged")
        fence = self.installation / "maintenance.json"
        if fence.exists():
            self._validate_fence()
            return False
        try:
            with (
                _file_lock(self.installation / "activity.lock", exclusive=False),
                _file_lock(self.state / "update-activity.lock", exclusive=False),
            ):
                if fence.exists():
                    self._validate_fence()
                    return False
                registration = _registration(self.state)
                if registration.get("external") == "docker" and registration.get("view") in {
                    "container",
                    "shared",
                }:
                    self._validate_docker_activation()
                    return True
                journal = self._journal()
                generation = _read(self.installation / "generation.json")
                version_key = "target" if self.request["outcome"] == "committed" else "source"
                if (
                    journal.get("phase") != self.request["outcome"]
                    or _release(str(journal.get(version_key))) != _release(self.version)
                    or _release(str(generation.get("version"))) != _release(self.version)
                    or registration.get("installation") != str(self.installation)
                ):
                    raise StartupRejected("Resident generation was not committed")
                return True
        except UpdateBusy:
            return False

    def wait(self, *, timeout: float = 300, cancelled: Callable[[], bool] | None = None) -> None:
        deadline = time.monotonic() + timeout
        while not self.committed():
            if cancelled is not None and cancelled():
                raise StartupRejected("Resident startup was cancelled")
            if time.monotonic() >= deadline:
                raise StartupRejected("Resident startup commit timed out")
            time.sleep(0.05)

    async def wait_async(self, *, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        while not self.committed():
            if time.monotonic() >= deadline:
                raise StartupRejected("Resident startup commit timed out")
            await asyncio.sleep(0.05)
