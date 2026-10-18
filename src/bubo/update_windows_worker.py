"""Retained, stdlib-only Windows detached-worker guardian.

Run single-threaded under -I -S from the retained helper. Never resume application
code or unwind admission after publishing retirement. The parent owns the Job
until exact receipt handoff succeeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path

from .update_locking import lock_file
from .update_platform import current_owner
from .update_windows_jobs import JobOwnership, existing_review, serialized_review_launch
from .update_windows_process import GuardianJob, JobProcess


def _generation(installation):
    try:
        return json.loads((installation / "generation.json").read_text())
    except FileNotFoundError:
        return None  # A newly registered installation has not been updated yet.


def _admission(payload, leases):
    installation = Path(payload["installation"]).resolve()
    state = Path(payload["state"]).resolve()
    environment = Path(payload["environment"]).resolve()
    # Open existing admission inodes only. Creating a missing lock here could
    # silently admit a different installation or bypass a removed registration.
    # Only registered launches use this guardian; installation/state leases below
    # replace the initial config bridge without holding the environment open.
    for path in (installation / "activity.lock", state / "update-activity.lock"):
        stream = path.open("rb")
        leases.append(stream)
        lock_file(stream.fileno(), exclusive=False)
    registration = json.loads((installation / "installation.json").read_text())
    state_registration = json.loads((state / "update-registration.json").read_text())
    if (
        registration.get("protocol_version") != 1
        or registration.get("owner") != current_owner()
        or registration.get("environment") != str(environment)
        or str(state) not in registration.get("states", [])
        or state_registration.get("installation") != str(installation)
        or state_registration.get("ready") is not True
        or "external" in state_registration
        or (installation / "maintenance.json").exists()
        or _generation(installation) != payload["generation"]
    ):
        raise OSError("Windows worker admission changed before handoff")
    return installation


def run(payload):
    guardian = GuardianJob(payload["parent"]["job"])
    leases = []
    owner = None
    status = 1
    try:
        installation = _admission(payload, leases)
        owner = JobOwnership.adopt(installation, payload["parent"], guardian)
        # The normal worker inherits the same non-breakaway Job, not the guardian's
        # lock handles. Admission remains held even after the worker root exits.
        try:
            process = subprocess.Popen(
                payload["args"],
                stdin=subprocess.DEVNULL,
                stdout=sys.stdout,
                stderr=sys.stderr,
                close_fds=True,
            )
        except OSError as error:
            code = (
                "missing_executable"
                if isinstance(error, FileNotFoundError)
                else "permission_denied"
                if isinstance(error, PermissionError)
                else "launch_failed"
            )
            owner.worker_failed(code)
            raise
        owner.worker_started()
        status = process.wait()
        while not guardian.alone():
            time.sleep(0.05)
    finally:
        if owner is not None:
            owner.finish_guardian(guardian, status)
        guardian.exit(1)


def main(payload_key):
    # Credentials/arguments never enter the ownership journal. Remove the private
    # launch envelope before starting the ordinary worker process.
    run(json.loads(os.environ.pop(payload_key)))


def spawn(
    args,
    installation: Path,
    state: Path,
    *,
    stdout,
    timeout=10,
    env=None,
    cwd=None,
    process_version=None,
):
    """Launch under parent admission and return only after durable guardian handoff.

    Called by the application or resident broker, never the retained guardian.
    Ordinary dispatch stays gated until Windows update registration is enabled.
    """
    import msvcrt

    from .update_admission import _check_generation

    installation, state = installation.resolve(), state.resolve()
    registration = json.loads((installation / "installation.json").read_text())
    archive = Path(registration.get("archive", installation / "helper.pyz")).resolve()
    if not archive.is_relative_to(installation) or not archive.is_file():
        raise OSError("Windows worker retained helper is unavailable")
    payload = {
        "installation": str(installation),
        "state": str(state),
        "environment": registration["environment"],
        "generation": _generation(installation),
        "args": [os.fspath(arg) for arg in args],
    }
    leases = []
    owner = child = None
    committed = False
    launch_lock = ExitStack()
    try:
        _admission(payload, leases)
        if process_version is None:
            _check_generation(installation)
        elif payload["generation"] is not None and (
            payload["generation"]["version"].split("+", 1)[0] != process_version.split("+", 1)[0]
        ):
            raise OSError("Windows resident version changed before worker launch")
        review_key = None
        if len(payload["args"]) >= 2 and payload["args"][-2] == "--worker":
            job_path = (Path(cwd or os.getcwd()) / payload["args"][-1]).resolve()
            review_key = hashlib.sha256(f"{state}\0{job_path}".encode()).hexdigest()
            launch_lock.enter_context(serialized_review_launch(installation))
            previous = existing_review(installation, review_key)
            if previous is not None:
                committed = True
                return previous
        owner = JobOwnership(installation, worker=True, review_key=review_key)
        payload["parent"] = {**owner.record, "job_created": True}
        key = "BUBO_WORKER_HANDOFF_" + uuid.uuid4().hex
        guardian_env = dict(os.environ if env is None else env)
        guardian_env.pop("BUBO_RESIDENT_CONTROL_STDIN", None)
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from bubo.update_windows_worker import main; main(sys.argv[2])"
        )
        with open(os.devnull, "rb") as stdin:
            child = JobProcess(
                [registration["python"], "-I", "-S", "-c", code, str(archive), key],
                name=owner.name,
                on_job_created=owner.created,
                handles=tuple(
                    msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, stdout, stdout)
                ),
                env={**guardian_env, key: json.dumps(payload)},
                cwd=cwd,
            )
        deadline = time.monotonic() + timeout
        while not owner.worker_ready(child):
            if child.poll() is not None:
                raise OSError("Windows worker guardian exited before adopting its launch")
            if time.monotonic() >= deadline:
                raise TimeoutError("Windows worker guardian handoff timed out")
            time.sleep(0.02)
        try:
            owner.handoff(child)
        except Exception:
            if child.job is not None:
                raise
            # Native handle relinquishment is the commit boundary even if later
            # acknowledgement bookkeeping fails unexpectedly. Never invite retry.
        committed = True
        return child.pid
    finally:
        committed = committed or (child is not None and child.job is None)
        try:
            if owner is not None and not committed:
                if child is None:
                    owner.close()
                else:
                    owner.abort_handoff(child)
            elif child is not None:
                with suppress(OSError):
                    child.close()  # Only a leftover process handle; Job was detached.
        finally:
            try:
                if committed:
                    with suppress(OSError):
                        launch_lock.close()
                else:
                    launch_lock.close()
            finally:
                for stream in reversed(leases):
                    if committed:
                        with suppress(OSError):
                            stream.close()
                    else:
                        stream.close()
