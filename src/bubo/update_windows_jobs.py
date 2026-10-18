"""Per-installation Windows Job ownership for admission and crash recovery.

Callers retain shared application admission while publishing/retiring records.
The updater may drain only while holding exclusive installation admission. This
protocol does not retroactively account for processes launched before registration.
"""

from __future__ import annotations

import errno
import json
import os
import re
from contextlib import contextmanager, suppress
from pathlib import Path

from bubo.update_identity import atomic_json
from bubo.update_platform import current_owner, windows_process_identity
from bubo.update_windows_process import _name, job_quiescent, new_job_name, recover_job

_WORKER_ERRORS = (None, "missing_executable", "permission_denied", "launch_failed")


class JobsBusy(OSError):
    """An admitted owner can still launch or use its recorded Job."""


def _directory(installation: Path, *, create=False) -> Path:
    directory = installation / "windows-jobs"
    if directory.is_symlink() or directory.is_junction():
        raise OSError("Windows job registry cannot redirect storage")
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    if directory.exists() and not directory.is_dir():
        raise OSError("Windows job registry is not a directory")
    return directory


def _validate(path: Path, record: dict, installation: Path) -> None:
    if (
        not isinstance(record, dict)
        or record.get("protocol") != 1
        or record.get("installation") != str(installation.resolve())
        or record.get("owner_sid") != current_owner()
        or type(record.get("owner_pid")) is not int
        or not 0 < record["owner_pid"] <= 0xFFFFFFFF
        or not isinstance(record.get("owner_start"), str)
        or not record["owner_start"]
        or type(record.get("job_created")) is not bool
        or record.get("state") not in {"pending", "abandoned", "quiescent"}
    ):
        raise OSError("Invalid Windows job ownership")
    try:
        name = _name(record.get("job"))
    except ValueError as error:
        raise OSError("Invalid Windows job identity") from error
    if path.name != name[-32:] + ".json":
        raise OSError("Windows job receipt identity changed")
    if "review_key" in record and (
        not isinstance(record["review_key"], str)
        or not re.fullmatch("[0-9a-f]{64}", record["review_key"])
        or "worker_started" not in record
    ):
        raise OSError("Invalid Windows review launch identity")
    if ("worker_started" in record or "worker_error" in record) and (
        type(record.get("worker_started")) is not bool
        or "worker_error" not in record
        or record["worker_error"] not in _WORKER_ERRORS
        or (record["worker_started"] and record["worker_error"] is not None)
    ):
        raise OSError("Invalid Windows worker startup receipt")


class JobOwnership:
    """Own one launch; no arguments, environment or command output are persisted."""

    def __init__(self, installation: Path, *, worker=False, review_key=None):
        self.installation = installation.resolve()
        start = windows_process_identity(os.getpid())
        if not start:
            raise OSError("Windows job owner identity unavailable")
        self.name = new_job_name()
        self.path = _directory(self.installation, create=True) / (self.name[-32:] + ".json")
        if self.path.exists() or self.path.is_symlink():
            raise OSError("Windows job receipt already exists")
        self.record = {
            "protocol": 1,
            "installation": str(self.installation),
            "job": self.name,
            "owner_sid": current_owner(),
            "owner_pid": os.getpid(),
            "owner_start": start,
            "job_created": False,
            "state": "pending",
        }
        self._launch_allowed = True
        self._creation_attempted = False
        if worker:
            self.record.update(worker_started=False, worker_error=None)
        if review_key is not None:
            self.record["review_key"] = review_key
            _validate(self.path, self.record, self.installation)
        atomic_json(self.path, self.record)

    def _publish(self, **changes):
        if json.loads(self.path.read_text()) != self.record:
            raise OSError("Windows job ownership changed")
        record = {**self.record, **changes}
        atomic_json(self.path, record)
        self.record = record

    def created(self):
        """Callback runs after exclusive named-job creation, before any child."""
        if not self._launch_allowed or self.record["state"] != "pending":
            raise OSError("Windows job launch rights have been relinquished")
        self._launch_allowed = False
        self._creation_attempted = True
        self._publish(job_created=True)

    @classmethod
    def adopt(cls, installation: Path, parent: dict, guardian):
        """Transfer an exact parent receipt after acquiring guardian admission.

        The parent must stop the entire Job before attempting cleanup; it cannot
        publish another record concurrently with this transfer. On any failure the
        guardian must exit natively while retaining its Job handle and leases.
        """
        installation = installation.resolve()
        name = _name(parent.get("job"))
        path = _directory(installation) / (name[-32:] + ".json")
        _validate(path, parent, installation)
        start = windows_process_identity(os.getpid())
        if (
            not start
            or parent["owner_pid"] == os.getpid()
            or not parent["job_created"]
            or parent["state"] != "pending"
            or guardian.name != name
            or not guardian.alone()
        ):
            raise OSError("Windows guardian cannot adopt this launch")
        owner = cls.__new__(cls)
        owner.installation, owner.name, owner.path = installation, name, path
        owner.record = dict(parent)
        owner._launch_allowed = False
        owner._creation_attempted = False
        owner._publish(owner_pid=os.getpid(), owner_start=start)
        return owner

    def acknowledge(self, child):
        """Validate the exact child transfer before releasing parent Job handles."""
        start = child.identity()
        transferred = {**self.record, "owner_pid": child.pid, "owner_start": start}
        if "worker_started" in transferred:
            transferred["worker_started"] = True
        current = json.loads(self.path.read_text())
        _validate(self.path, current, self.installation)
        if (
            not start
            or child.pid == self.record["owner_pid"]
            or child.name != self.name
            or not self.record["job_created"]
            or self.record["state"] != "pending"
            or self.record["owner_pid"] != os.getpid()
            or windows_process_identity(os.getpid()) != self.record["owner_start"]
            or current
            not in (
                transferred,
                {**transferred, "state": "quiescent"},
            )
        ):
            raise OSError("Windows guardian handoff is not acknowledged")
        self._handoff_record = transferred

    def worker_started(self):
        self._worker_status(started=True)

    def worker_failed(self, code):
        if code not in _WORKER_ERRORS[1:]:
            raise ValueError("Unknown Windows worker launch failure")
        self._worker_status(error=code)

    def _worker_status(self, *, started=False, error=None):
        if (
            self.record.get("worker_started") is not False
            or self.record.get("worker_error") is not None
            or self.record["owner_pid"] != os.getpid()
            or windows_process_identity(os.getpid()) != self.record["owner_start"]
            or self.record["state"] != "pending"
        ):
            raise OSError("Windows worker startup owner changed")
        self._publish(worker_started=started, worker_error=error)

    def worker_ready(self, child):
        current = json.loads(self.path.read_text())
        _validate(self.path, current, self.installation)
        if current == self.record:
            return False
        transferred = {**self.record, "owner_pid": child.pid, "owner_start": child.identity()}
        if child.name != self.name or current not in self._transfer_records(transferred):
            raise OSError("Windows worker startup receipt changed")
        error = current.get("worker_error")
        if error:
            if error == "missing_executable":
                raise FileNotFoundError(errno.ENOENT, "Windows worker executable was not found")
            if error == "permission_denied":
                raise PermissionError(errno.EACCES, "Windows worker executable permission denied")
            raise OSError("Windows worker process creation failed")
        if current["state"] == "abandoned":
            raise OSError("Windows worker guardian abandoned startup")
        if current.get("worker_started") is True:
            return True
        if current["state"] == "quiescent":
            raise OSError("Windows worker guardian exited without startup acknowledgement")
        return False

    @staticmethod
    def _transfer_records(transferred):
        starts = [transferred]
        if "worker_started" in transferred:
            starts = [
                {**transferred, "worker_started": False, "worker_error": code}
                for code in _WORKER_ERRORS
            ] + [{**transferred, "worker_started": True, "worker_error": None}]
        return [
            {**record, "state": state}
            for record in starts
            for state in ("pending", "abandoned", "quiescent")
        ]

    def handoff(self, child):
        """Commit handle transfer; acknowledgement cleanup cannot fail the launch."""
        child.detach(self.acknowledge)
        # The guardian may already have finished. It retains its terminal receipt
        # until this marker is present, so fast completion cannot look like failure.
        try:
            atomic_json(self.path.with_suffix(".ack"), self._handoff_record)
            _retire_handoff(self.path, self._handoff_record)
        except OSError, ValueError:
            # Handoff is committed, even if cleanup is unavailable. Exclusive
            # recovery can remove the quiescent receipt and non-authoritative ack.
            pass

    def abort_handoff(self, child):
        """Stop the exact launch before reconciling either side of a transfer."""
        self._launch_allowed = False
        if (
            child.name != self.name
            or self.record["owner_pid"] != os.getpid()
            or windows_process_identity(os.getpid()) != self.record["owner_start"]
        ):
            raise OSError("Windows handoff cancellation owner changed")
        try:
            start = child.identity()
        except BaseException as error:
            child.close()
            raise OSError(
                "Windows stopped handoff identity is unavailable; restart the owning Bubo process"
            ) from error
        try:
            child.close()  # No receipt writes while the guardian might still run.
        except BaseException as error:
            raise OSError(
                "Windows worker handoff cleanup unresolved; restart the owning Bubo process "
                "before retrying the launch or updates"
            ) from error
        current = json.loads(self.path.read_text())
        transferred = {**self.record, "owner_pid": child.pid, "owner_start": start}
        _validate(self.path, current, self.installation)
        if current != self.record and current not in self._transfer_records(transferred):
            raise OSError("Windows stopped handoff receipt changed")
        # Whole-job quiescence, not the old parent's identity alone, authorizes
        # retirement of the transferred receipt. Nothing can adopt after this point.
        self.record = current
        self._publish(state="quiescent")
        with suppress(OSError):
            self.path.unlink()
            self.path.with_suffix(".ack").unlink(missing_ok=True)

    def finish_guardian(self, guardian, status: int):
        """Retire a non-spawning guardian and exit with all admission still held.

        This is terminal, including on publication failure. The retained minimal
        guardian calls this only after its last spawn; no application cleanup may
        execute after the sole-member check. Uncertain publication keeps recovery
        evidence, never permission to resume work or release admission normally.
        """
        result = 1
        try:
            if (
                guardian.name != self.name
                or self.record["owner_pid"] != os.getpid()
                or windows_process_identity(os.getpid()) != self.record["owner_start"]
                or self.record["state"] != "pending"
            ):
                raise OSError("Windows guardian ownership changed")
            if not guardian.alone():
                self._publish(state="abandoned")
            else:
                self._publish(state="quiescent")
                _retire_handoff(self.path, self.record)
                result = status
        finally:
            guardian.exit(result)

    def close(self, child=None):
        """Retire only after the abandoned launch/tree is proven stopped."""
        self._launch_allowed = False
        current = json.loads(self.path.read_text())
        if (
            self._creation_attempted
            and not self.record["job_created"]
            and current == {**self.record, "job_created": True}
        ):
            # Our exclusive creation callback can publish and then report an
            # uncertain flush. It never returned, so no workload was launched.
            # Accept only that exact transition, never other receipt changes.
            self.record = current
        if (
            self.record["owner_pid"] != os.getpid()
            or windows_process_identity(os.getpid()) != self.record["owner_start"]
            or current != self.record
        ):
            raise OSError(
                "Windows job owner changed; restart the owning Bubo process before retrying updates"
            )
        if child is not None and child.name != self.name:
            raise OSError("Windows job handle identity changed")
        try:
            if child is not None:
                child.close()  # JobProcess verifies zero members before closing.
            elif self.record["job_created"]:
                # Construction failed after our exclusive creation callback. This
                # caller owns the abandoned launch; never touch a name collision.
                recover_job(self.name)
            self._publish(state="quiescent")
        except BaseException:
            # Admission remains held until the caller receives this failure. A
            # durable abandonment lets the next exclusive owner finish cleanup
            # even if this MCP/service process survives. Launch rights never return.
            try:
                self._publish(state="abandoned")
            except Exception as error:
                raise OSError(
                    "Windows job cleanup is unresolved; restart the owning Bubo "
                    "process before retrying updates"
                ) from error
            raise
        # A lost deletion can only resurrect a quiescent receipt. Avoid growing
        # the ledger when automatic updates are disabled and never drain it.
        with suppress(OSError):
            self.path.unlink()


def _retire_handoff(path: Path, record: dict):
    """Either side can retire after completion AND parent acknowledgement."""
    marker = path.with_suffix(".ack")
    try:
        if json.loads(marker.read_text()) != {**record, "state": "pending"}:
            return
        if json.loads(path.read_text()) != {**record, "state": "quiescent"}:
            return
        path.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
    except FileNotFoundError:
        pass  # Peer cleanup, or completion/acknowledgement has not happened yet.


@contextmanager
def serialized_review_launch(installation: Path):
    """Serialize duplicate checks through handoff; caller already holds admission."""
    from bubo.update_locking import lock_file

    path = installation / "worker-launch.lock"
    if path.is_symlink() or path.is_junction():
        raise OSError("Windows worker launch lock storage changed")
    with path.open("a+b") as stream:
        lock_file(stream.fileno(), exclusive=True)
        yield


def existing_review(installation: Path, key: str) -> int | None:
    """Reuse a live guardian, or defer uncertain ownership rather than duplicate.

    Requires serialized_review_launch plus shared application admission. This
    path must not terminate Jobs: a guardian can still be adopting a dead parent's
    receipt. Exclusive updater recovery remains the authority to stop such Jobs.
    """
    directory = _directory(installation)
    if not directory.exists():
        return None
    for path in directory.glob("*.json"):
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise OSError("Windows job receipt storage changed")
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            continue  # A completed guardian may retire its receipt concurrently.
        _validate(path, record, installation)
        if record.get("review_key") != key or record["state"] == "quiescent":
            continue
        alive = windows_process_identity(record["owner_pid"]) == record["owner_start"]
        if alive:
            if record["state"] == "pending" and record.get("worker_started") is True:
                return record["owner_pid"]
            raise JobsBusy("Previous Windows review launch has unresolved ownership")
        try:
            if json.loads(path.read_text()) != record:
                raise JobsBusy("Previous Windows review ownership is changing")
        except FileNotFoundError:
            continue
        if record["job_created"] and not job_quiescent(record["job"]):
            raise JobsBusy("Previous Windows review descendants are still active")
        # A dead owner cannot create a new Job; a confirmed empty Job has nobody
        # left who can adopt or spawn. Leave receipt retirement to its normal owner.
    return None


def drain(installation: Path) -> None:
    """Under exclusive admission, prove every recorded Job stopped or defer."""
    directory = _directory(installation)
    if not directory.exists():
        return
    markers = []
    for path in sorted(directory.iterdir()):
        # Atomic publication temporary files aren't receipts and contain no
        # authority to launch. Unexpected permanent entries are not ignored.
        if re.fullmatch(r"\.[0-9a-f]{32}\.(json|ack)\.[^.]+\.tmp", path.name):
            continue
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise OSError("Windows job receipt storage changed")
        if re.fullmatch(r"[0-9a-f]{32}\.ack", path.name):
            markers.append(path)
            continue  # Acknowledgements never authorize launch or recovery.
        try:
            record = json.loads(path.read_text())
            _validate(path, record, installation)
        except (ValueError, TypeError) as error:
            raise OSError("Windows job receipt is unreadable") from error
        if record["state"] != "quiescent":
            if (
                record["state"] == "pending"
                and windows_process_identity(record["owner_pid"]) == record["owner_start"]
            ):
                raise JobsBusy("Windows job owner is still active")
            if record["job_created"]:
                recover_job(record["job"])
            # A callback was never committed for an uncreated Job: its protocol
            # guarantees no workload could start, even if creation was interrupted.
        path.unlink()
        # Reappearing deletion after a crash is harmless: next drain repeats the
        # same quiescence proof. Publication/directory durability is a separate gate.
    for marker in markers:
        marker.unlink(missing_ok=True)
