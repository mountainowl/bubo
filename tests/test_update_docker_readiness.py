"""Docker readiness protocol fixtures; live daemon acceptance is separate."""

import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from bubo.update_docker_readiness import _PROCESS_CHECK, DockerReadiness
from bubo.update_identity import atomic_json
from bubo.update_readiness import StartupRejected


class CandidateAPI:
    def __init__(self, request_id):
        self.daemon = "owned-daemon"
        self.container = {
            "Id": "a" * 64,
            "Image": "sha256:" + "b" * 64,
            "Config": {"Labels": {"io.bubo.update.request": request_id}},
            "HostConfig": {"RestartPolicy": {"Name": "no"}},
            "State": {"Running": True, "StartedAt": "2026-10-16T16:00:00.0001Z", "Pid": 91827},
        }
        self.execution = {
            "ID": "c" * 64,
            "ContainerID": "a" * 64,
            "Running": False,
            "ExitCode": 0,
        }
        self.calls = []
        self.restart_during_probe = False

    def inspect_container(self, identifier):
        assert identifier == "a" * 64
        return copy.deepcopy(self.container)

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/info":
            return {"ID": self.daemon}
        if path.endswith("/exec"):
            assert body["Cmd"][:2] == ["python", "-c"]
            return {"Id": "c" * 64}
        if path.endswith("/start"):
            assert body == {"Detach": True, "Tty": False}
            return None
        if path.endswith("/json"):
            if self.restart_during_probe:
                self.container["State"]["StartedAt"] = "2026-10-16T16:00:00.0002Z"
            return copy.deepcopy(self.execution)
        raise AssertionError((method, path, body))


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    root = tmp_path / "host"
    state = root / "var/state"
    installation = root / ".updates/installation"
    state.mkdir(parents=True)
    installation.mkdir(parents=True)
    atomic_json(
        state / "update-registration.json",
        {
            "protocol_version": 1,
            "external": "docker",
            "mapping": {
                "host": {"root": str(root), "state": str(state), "installation": str(installation)},
                "container": {
                    "root": "/data/bubo",
                    "state": "/data/bubo/var/state",
                    "installation": "/data/bubo/.updates/installation",
                },
            },
        },
    )
    journal = {
        "attempt_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "states": [str(state)],
        "phase": "readiness",
        "target": "0.28.0",
        "source": "0.27.0",
    }
    atomic_json(installation / "attempt.json", journal)
    atomic_json(installation / "maintenance.json", journal)
    api = CandidateAPI("")
    readiness = DockerReadiness(api, state, "service")
    api.container["Config"]["Labels"]["io.bubo.update.request"] = readiness.request_id
    readiness.bind(api.container["Id"], api.container["Image"])

    def forbidden_host_pid(*args):
        raise AssertionError("Docker PIDs must not be resolved in the host namespace")

    monkeypatch.setattr("bubo.update_readiness._start_identity", forbidden_host_pid)
    return readiness, api


def acknowledge(readiness, **changes):
    receipt = {
        **readiness.request,
        "pid": 7,
        "loaded_version": "0.28.0",
        "process_identity": "proc:00000000-0000-0000-0000-000000000001:1234",
        **changes,
    }
    atomic_json(readiness.ready_path, receipt)
    readiness.path.with_suffix(".claim").touch()


def test_live_container_receipt_proves_process_in_container_namespace(candidate):
    readiness, api = candidate
    assert readiness.verify() is False
    acknowledge(readiness)
    assert readiness.verify()
    execution = next(body for method, path, body in api.calls if path.endswith("/exec"))
    assert execution["Cmd"][-2] == "7"
    assert "91827" not in str(execution)
    assert "BUBO_MAINTENANCE_TOKEN" not in readiness.environment(
        {"BUBO_MAINTENANCE_TOKEN": "secret"}
    )


def test_controller_recovers_same_live_daemon_candidate(candidate):
    readiness, api = candidate
    acknowledge(readiness)
    recovered = DockerReadiness.recover(api, readiness.state, readiness.request_id)
    assert recovered.verify()
    assert recovered.request == readiness.request


@pytest.mark.parametrize(
    "change", ["daemon", "image", "restart", "paused", "dead", "label", "policy", "pid-mode"]
)
def test_daemon_or_container_ownership_change_rejects_receipt(candidate, change):
    readiness, api = candidate
    acknowledge(readiness)
    if change == "daemon":
        api.daemon = "other-daemon"
    elif change == "image":
        api.container["Image"] = "sha256:" + "d" * 64
    elif change == "restart":
        api.container["State"]["StartedAt"] = "2026-10-16T17:00:00Z"
    elif change in {"paused", "dead"}:
        api.container["State"][change.title()] = True
    elif change == "label":
        api.container["Config"]["Labels"].clear()
    elif change == "policy":
        api.container["HostConfig"]["RestartPolicy"] = {"Name": "always"}
    else:
        api.container["HostConfig"]["PidMode"] = "host"
    with pytest.raises(StartupRejected, match="Docker candidate"):
        readiness.verify()
    assert not any(path.endswith("/exec") for _, path, _ in api.calls)


def test_restart_during_namespace_probe_invalidates_success(candidate):
    readiness, api = candidate
    acknowledge(readiness)
    api.restart_during_probe = True
    with pytest.raises(StartupRejected, match="no longer"):
        readiness.verify()


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": 0},
        {"pid": True},
        {"loaded_version": "0.27.0"},
        {"token": "old"},
        {"process_identity": "host-pid"},
    ],
)
def test_invalid_receipt_never_authorizes_commit(candidate, changes):
    readiness, _ = candidate
    acknowledge(readiness, **changes)
    with pytest.raises(StartupRejected):
        readiness.verify()


@pytest.mark.parametrize("change", ["exit", "foreign"])
def test_namespace_probe_must_succeed_for_exact_container(candidate, change):
    readiness, api = candidate
    acknowledge(readiness)
    if change == "exit":
        api.execution["ExitCode"] = 1
    else:
        api.execution["ContainerID"] = "d" * 64
    with pytest.raises(StartupRejected, match="namespace"):
        readiness.verify()


def test_readiness_requires_matching_durable_owner_and_claim(candidate):
    readiness, _ = candidate
    acknowledge(readiness)
    readiness.path.with_suffix(".claim").unlink()
    with pytest.raises(StartupRejected, match="readiness"):
        readiness.verify()
    acknowledge(readiness)
    record = json.loads(readiness.owner_path.read_text())
    record["started_at"] = "changed"
    atomic_json(readiness.owner_path, record)
    with pytest.raises(StartupRejected, match="ownership record"):
        readiness.verify()


@pytest.mark.parametrize("state", ["Z", "X", "x", "S", "R"])
def test_namespace_check_requires_live_process_state(monkeypatch, state):
    boot = "00000000-0000-0000-0000-000000000001"
    fields = [state] + ["0"] * 18 + ["1234"]
    files = {
        "/proc/7/stat": "7 (python worker) " + " ".join(fields),
        "/proc/sys/kernel/random/boot_id": boot,
    }
    monkeypatch.setattr(Path, "read_text", lambda path: files[str(path)])
    monkeypatch.setattr(sys, "argv", ["-c", "7", "proc:" + boot + ":1234"])
    with pytest.raises(SystemExit) as result:
        exec(_PROCESS_CHECK, {})
    assert bool(result.value.code) == (state in {"Z", "X", "x"})


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires real Linux procfs")
def test_real_unreaped_resident_cannot_acknowledge_readiness(candidate, monkeypatch):
    from bubo.update_installers import _start_identity

    readiness, api = candidate
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        acknowledge(readiness, pid=child.pid, process_identity=_start_identity(child.pid))
        request = api.request
        command = []

        def execute_probe(method, path, body=None):
            if path.endswith("/exec"):
                command[:] = [sys.executable, *body["Cmd"][1:]]
            if path.endswith("/start"):
                api.execution["ExitCode"] = subprocess.run(command, timeout=3).returncode
            return request(method, path, body)

        monkeypatch.setattr(api, "request", execute_probe)
        with pytest.raises(StartupRejected, match="namespace process"):
            readiness.verify()
    finally:
        child.wait(timeout=3)
