"""Verify a host/container storage boundary before registering its controller."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath

from bubo.update_admission import _file_lock
from bubo.update_docker import DockerAPI
from bubo.update_installers import UnsafeInstallation, stable_version

_PROBE = """
import fcntl, json, os, pathlib, sys
from importlib.metadata import version
import bubo.update_admission
assert bubo.update_admission.UNREGISTERED_STATE_LEASE_VERSION == 2
directory, nonce, uid, expected = sys.argv[1:]
directory = pathlib.Path(directory)
assert os.getuid() == int(uid)
assert (directory / 'nonce').read_text() == nonce
state_descriptor = os.open(directory.parent, os.O_RDONLY)
try:
    try:
        fcntl.flock(state_descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError('State directory lease is not shared')
finally:
    os.close(state_descriptor)
with (directory / 'lock').open('rb') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = False
    except BlockingIOError:
        locked = True
    assert locked == (expected == 'locked')
    (directory / 'result').write_text(json.dumps({
        'nonce': nonce, 'uid': os.getuid(), 'locked': locked, 'version': version('bubo'),
    }))
"""


def probe_storage(
    api: DockerAPI, descriptor: dict, *, timeout: float = 5, state_locked: bool = False
) -> str:
    """Prove nonce visibility and lock contention in both directions of setup.

    Only a private temporary directory is touched. An interrupted exec cannot
    touch application state or hold its activity locks.
    """
    if not state_locked:
        with _file_lock(Path(descriptor["host_state"]), exclusive=True):
            return probe_storage(api, descriptor, timeout=timeout, state_locked=True)
    identifier = descriptor["container_id"]
    original = api.inspect_container(identifier)
    daemon = api.request("GET", "/info").get("ID")

    def validate():
        current = api.inspect_container(identifier)
        state = current.get("State", {})
        if (
            not daemon
            or api.request("GET", "/info").get("ID") != daemon
            or current.get("Id") != identifier
            or current.get("Image") != descriptor["image_id"]
            or state.get("Running") is not True
            or state.get("Paused")
            or state.get("Restarting")
            or state.get("Dead")
            or not state.get("StartedAt")
            or state["StartedAt"] != original.get("State", {}).get("StartedAt")
        ):
            raise UnsafeInstallation("Docker setup requires the same running source container")

    validate()
    state = Path(descriptor["host_state"])
    with tempfile.TemporaryDirectory(prefix=".bubo-update-probe-", dir=state) as directory:
        directory = Path(directory)
        nonce = str(uuid.uuid4())
        (directory / "nonce").write_text(nonce)
        (directory / "lock").touch(mode=0o600)
        mapped = PurePosixPath(descriptor["container_state"]) / directory.name

        def run(locked: bool):
            validate()
            (directory / "result").unlink(missing_ok=True)
            execution = api.request(
                "POST",
                "/containers/" + identifier + "/exec",
                {
                    "Cmd": [
                        "python",
                        "-c",
                        _PROBE,
                        str(mapped),
                        nonce,
                        str(os.getuid()),
                        "locked" if locked else "free",
                    ],
                    "AttachStdin": False,
                    "AttachStdout": False,
                    "AttachStderr": False,
                },
            ).get("Id", "")
            if not re.fullmatch(r"[0-9a-f]{64}", execution):
                raise UnsafeInstallation("Docker storage verification could not start")
            api.request("POST", "/exec/" + execution + "/start", {"Detach": True, "Tty": False})
            deadline = time.monotonic() + timeout
            while True:
                result = api.request("GET", "/exec/" + execution + "/json")
                if result.get("ID") != execution or result.get("ContainerID") != identifier:
                    raise UnsafeInstallation("Docker storage verification ownership changed")
                if result.get("Running") is False:
                    if type(result.get("ExitCode")) is not int or result["ExitCode"] != 0:
                        raise UnsafeInstallation(
                            "Docker shared storage, UID or locking verification failed"
                        )
                    break
                if time.monotonic() >= deadline:
                    raise UnsafeInstallation("Docker storage verification timed out")
                time.sleep(0.05)
            validate()
            receipt = json.loads((directory / "result").read_text())
            if (
                receipt.get("nonce") != nonce
                or receipt.get("uid") != os.getuid()
                or receipt.get("locked") is not locked
            ):
                raise UnsafeInstallation("Docker shared storage verification did not match")
            stable_version(receipt["version"])
            return receipt["version"]

        with _file_lock(directory / "lock", exclusive=True):
            version = run(True)
        if run(False) != version:
            raise UnsafeInstallation("Docker source version changed during setup")
        return version


def configure(
    root: Path, container: str, socket_path: Path, *, home: Path | None = None, runner=None
) -> dict:
    """Retain sanitized setup failures as local reports and consented outbox events."""
    context = {}
    try:
        return _configure(root, container, socket_path, home=home, runner=runner, context=context)
    except Exception as error:
        from bubo import update_helper as helper
        from bubo.analytics_config import AnalyticsConfig
        from bubo.update_reporting import FailureStore

        try:
            state = context.get("state", root / ".updates/setup-failures")
            config = (
                helper.analytics_config_from_dict(helper._raw_config(state, root=root))
                if "state" in context
                else AnalyticsConfig(enabled=False)
            )
            store = FailureStore(state)
            attempt = str(uuid.uuid4())
            store.record(
                attempt_id=attempt,
                phase="bootstrap",
                error_code="controller_unavailable",
                cfg=config,
                install_type="docker",
                producer_version=helper.installed_version(),
                consent=helper.analytics_enabled(config),
            )
            report = store.directory / "setup-report.md"
            report.write_text(store.report(attempt))
            error.bubo_report = str(report)
        except Exception:
            # The failure report is best-effort; the original setup error still propagates.
            pass
        raise


def _configure(
    root: Path, container: str, socket_path: Path, *, home: Path | None, runner, context: dict
) -> dict:
    """Register a verified external controller without changing update consent."""
    import subprocess

    from bubo import update_helper as helper
    from bubo.update_docker import replacement_preflight
    from bubo.update_docker_runtime import resident_kind
    from bubo.update_identity import atomic_json

    if not root.is_absolute() or not socket_path.is_absolute():
        raise UnsafeInstallation("Docker setup requires absolute host root and socket paths")
    root = root.resolve()
    installation = root / ".updates" / "installation"
    if installation.resolve() != installation:
        raise UnsafeInstallation("Docker controller storage must not be redirected by symlinks")
    api = DockerAPI(socket_path)
    daemon = api.request("GET", "/info").get("ID")
    retained = frozenset()
    if (installation / "installation.json").exists():
        from bubo.update_docker_installer import DockerInstaller

        registered = helper._read(installation)
        if (
            registered.get("kind") != "docker"
            or len(registered["states"]) != 1
            or registered["docker"].get("daemon_id") != daemon
            or registered["docker"].get("socket") != str(socket_path)
        ):
            raise UnsafeInstallation("Docker controller ownership changed")
        retained = DockerInstaller(
            api,
            Path(registered["states"][0]),
            registered["docker"]["container_id"],
        ).retained_ids()
    descriptor = replacement_preflight(
        api,
        container,
        root,
        retained_ids=retained,
    )
    resident_kind(descriptor["create"], root)
    state = Path(descriptor["host_state"])
    context["state"] = state
    context["environment"] = dict(
        entry.split("=", 1) for entry in descriptor["create"].get("Env", []) if "=" in entry
    )
    if str(root) == descriptor["container_root"]:
        raise UnsafeInstallation("Host and container controller paths must be distinguishable")
    registration_path = state / "update-registration.json"
    registration = {
        "protocol_version": 1,
        "external": "docker",
        "docker": {"container_id": descriptor["container_id"]},
        "mapping": {
            "host": {"root": str(root), "state": str(state), "installation": str(installation)},
            "container": {
                "root": descriptor["container_root"],
                "state": descriptor["container_state"],
                "installation": str(
                    PurePosixPath(descriptor["container_root"]) / ".updates/installation"
                ),
            },
        },
    }
    installation.mkdir(parents=True, exist_ok=True, mode=0o700)
    if installation.stat().st_uid != os.getuid():
        raise UnsafeInstallation("Docker controller storage belongs to another user")
    for name in ("registry.lock", "activity.lock", "attempt.lock", "sender.lock"):
        (installation / name).touch(mode=0o600, exist_ok=True)
    (state / "update-activity.lock").touch(mode=0o600, exist_ok=True)
    with (
        _file_lock(installation / "attempt.lock", exclusive=True),
        _file_lock(installation / "registry.lock", exclusive=True),
        _file_lock(state, exclusive=True),
    ):
        if (installation / "maintenance.json").exists():
            raise UnsafeInstallation("Finish pending recovery before Docker setup")
        if registration_path.exists() and json.loads(registration_path.read_text()) != registration:
            raise UnsafeInstallation(
                "Persistent state already belongs to another update controller"
            )
        record_path = installation / "installation.json"
        docker = {
            "container_id": descriptor["container_id"],
            "daemon_id": daemon,
            "socket": str(socket_path),
        }
        previous = json.loads(record_path.read_text()) if record_path.exists() else None
        if previous is not None and (
            previous.get("kind") != "docker"
            or previous.get("docker") != docker
            or previous.get("owner") != os.getuid()
            or previous.get("states") != [str(state)]
        ):
            raise UnsafeInstallation("Docker controller ownership changed")
        version = probe_storage(api, descriptor, state_locked=True)
        python = Path(sys._base_executable).resolve()
        if not python.is_file():
            raise UnsafeInstallation("A retained host Python interpreter is required")
        record = {
            **(previous or {}),
            "protocol_version": 1,
            "kind": "docker",
            "docker": docker,
            "owner": os.getuid(),
            "python": str(python),
            "source_version": version,
            "helper_version": helper.installed_version(),
            "states": [str(state)],
            "ready": False,
            "next_check": (previous or {}).get("next_check", 0),
        }
        record["archive"] = helper._bundle(installation, python)
        atomic_json(record_path, record)
        atomic_json(registration_path, registration)
        record["schedule"] = helper.register_schedule(
            installation,
            record,
            home=home or Path.home(),
            runner=runner or subprocess.run,
        )
        record["ready"] = True
        atomic_json(record_path, record)
        return record
