"""Real-process lock probes behind a simulated Docker exec transport."""

import copy
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from bubo import update_docker_setup as setup
from bubo.update_installers import UnsafeInstallation


class ExecAPI:
    def __init__(self, descriptor, *, failure=None):
        self.descriptor, self.failure = descriptor, failure
        self.container = {
            "Id": descriptor["container_id"],
            "Image": descriptor["image_id"],
            "State": {"Running": True, "StartedAt": "start-1"},
        }
        self.calls = 0

    def inspect_container(self, identifier):
        assert identifier == self.container["Id"]
        return copy.deepcopy(self.container)

    def request(self, method, path, body=None):
        if path == "/info":
            return {"ID": "daemon"}
        if path.endswith("/exec"):
            assert body["AttachStdout"] is False
            assert body["AttachStderr"] is False
            self.command = [sys.executable, *body["Cmd"][1:]]
            if self.failure == "uid":
                self.command[-2] = "-1"
            elif self.failure == "nonce":
                self.command[-3] = "different"
            elif self.failure == "directory":
                source = Path(self.command[3])
                other = source.parent.with_name(source.parent.name + "-alternate")
                other.mkdir(exist_ok=True)
                (other / source.name).symlink_to(source, target_is_directory=True)
                self.command[3] = str(other / source.name)
            return {"Id": "e" * 64}
        if path.endswith("/start"):
            self.calls += 1
            self.code = subprocess.run(
                self.command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            ).returncode
            if self.failure == "restart":
                self.container["State"]["StartedAt"] = "start-2"
            return None
        assert path == "/exec/" + "e" * 64 + "/json"
        return {
            "ID": "e" * 64,
            "ContainerID": "changed" if self.failure == "owner" else self.container["Id"],
            "Running": self.failure == "timeout",
            "ExitCode": self.code,
        }


@pytest.mark.parametrize(
    "failure", [None, "uid", "nonce", "lock", "directory", "restart", "owner", "timeout", "stopped"]
)
def test_storage_probe_verifies_real_locks_and_cleans_private_files(tmp_path, monkeypatch, failure):
    descriptor = {
        "container_id": "c" * 64,
        "image_id": "sha256:" + "a" * 64,
        "host_state": str(tmp_path),
        "container_state": str(tmp_path),
    }
    api = ExecAPI(descriptor, failure=failure)
    if failure == "lock":
        monkeypatch.setattr(setup, "_file_lock", lambda *args, **kwargs: nullcontext())
    if failure == "stopped":
        api.container["State"]["Running"] = False
    if failure:
        with pytest.raises(UnsafeInstallation):
            setup.probe_storage(api, descriptor, timeout=0)
    else:
        from importlib.metadata import version

        assert setup.probe_storage(api, descriptor) == version("bubo")
        assert api.calls == 2
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    from bubo import update_docker, update_helper

    root = tmp_path / "root"
    state = root / "custom/state"
    state.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config/env.toml").write_text("auto_update=false\n[analytics]\nenabled=false\n")
    descriptor = {
        "container_id": "c" * 64,
        "image_id": "sha256:" + "a" * 64,
        "host_state": str(state),
        "container_state": "/data/bubo/custom/state",
        "container_root": "/data/bubo",
        "create": {"Entrypoint": ["bubo-poller"], "Cmd": ["service", "start", "--foreground"]},
    }
    api = ExecAPI(descriptor)
    monkeypatch.setattr(setup, "DockerAPI", lambda path: api)
    monkeypatch.setattr(update_docker, "replacement_preflight", lambda *args, **kwargs: descriptor)
    monkeypatch.setattr(setup, "probe_storage", lambda *args, **kwargs: "0.28.0")
    calls = []

    def schedule(installation, record, **kwargs):
        assert kwargs["home"] is not None
        assert json.loads((installation / "installation.json").read_text())["ready"] is False
        calls.append(record)
        return "fake-native-schedule"

    monkeypatch.setattr(update_helper, "register_schedule", schedule)
    return root, state, calls


def test_configuration_maps_custom_state_and_preserves_opt_out(configuration):
    from bubo.update_registration import load_registration

    root, state, calls = configuration
    result = setup.configure(root, "bubo", root / "docker.sock")
    assert result["ready"] is True
    assert result["kind"] == "docker"
    registration = load_registration(state)
    assert registration["view"] == "host"
    assert registration["mapping"]["container"]["state"] == "/data/bubo/custom/state"
    assert (root / "config/env.toml").read_text().startswith("auto_update=false")
    assert setup.configure(root, "bubo", root / "docker.sock")["ready"] is True
    assert len(calls) == 2


def test_scheduler_failure_is_retryable(configuration, monkeypatch):
    from bubo import update_helper

    root, _, _ = configuration
    schedule = update_helper.register_schedule
    monkeypatch.setattr(
        update_helper,
        "register_schedule",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("scheduler")),
    )
    with pytest.raises(OSError, match="scheduler"):
        setup.configure(root, "bubo", root / "docker.sock")
    record = root / ".updates/installation/installation.json"
    assert json.loads(record.read_text())["ready"] is False
    report = root / "custom/state/update-diagnostics/setup-report.md"
    assert "controller_unavailable" in report.read_text()
    monkeypatch.setattr(update_helper, "register_schedule", schedule)
    assert setup.configure(root, "bubo", root / "docker.sock")["ready"] is True


def test_configuration_refuses_conflicting_registration(configuration):
    root, state, calls = configuration
    marker = state / "update-registration.json"
    marker.write_text('{"installation":"other"}')
    with pytest.raises(UnsafeInstallation, match="another"):
        setup.configure(root, "bubo", root / "docker.sock")
    assert marker.read_text() == '{"installation":"other"}'
    assert calls == []


def test_cli_docker_configuration_dispatch(configuration):
    from bubo.cli import build_parser

    root, _, calls = configuration
    parser = build_parser()
    arguments = parser.parse_args(
        [
            "update",
            "configure",
            "--root",
            str(root),
            "--container",
            "bubo",
            "--docker-socket",
            str(root / "docker.sock"),
        ]
    )
    assert arguments.func(arguments) == 0
    assert len(calls) == 1
    arguments.root = None
    assert arguments.func(arguments) == 1
    assert len(calls) == 1


def test_first_registration_defers_while_unregistered_work_holds_state(configuration):
    from bubo.update_admission import UpdateBusy, _registered_activity

    root, state, calls = configuration
    with _registered_activity(state, writable=False):
        with pytest.raises(UpdateBusy):
            setup.configure(root, "bubo", root / "docker.sock")
    assert not (state / "update-registration.json").exists()
    assert calls == []
    assert setup.configure(root, "bubo", root / "docker.sock")["ready"]


def test_docker_schedulers_have_unique_stable_identity(tmp_path):
    from bubo.update_helper import _schedule_label

    first = tmp_path / "first/.updates/installation"
    second = tmp_path / "second/.updates/installation"
    record = {"kind": "docker"}
    assert _schedule_label(first, record) != _schedule_label(second, record)
    assert _schedule_label(first, record) == _schedule_label(first, record)


@pytest.mark.parametrize("opt_out", [False, True])
def test_setup_failure_outbox_honors_analytics_config(configuration, monkeypatch, opt_out):
    import sqlite3

    from bubo import update_helper

    root, state, _ = configuration
    (root / "config/env.toml").write_text(f"[analytics]\nenabled={str(not opt_out).lower()}\n")
    identity = "a28cc7c4-955e-4ff0-949e-c1826754f571"
    (state / "install_id").write_text(identity)

    def fail(*args, **kwargs):
        raise OSError("secret-that-must-not-be-logged")

    monkeypatch.setattr(update_helper, "register_schedule", fail)
    with pytest.raises(OSError, match="secret-that-must-not-be-logged"):
        setup.configure(root, "bubo", root / "docker.sock")
    with sqlite3.connect(state / "update-diagnostics/failures.sqlite") as connection:
        payload, status = connection.execute("SELECT payload,status FROM failures").fetchone()
    assert (status == "local") is opt_out
    if not opt_out:
        properties = json.loads(payload)["properties"]
        assert properties["distinct_id"] == properties["install_id"] == identity
        assert properties["bubo_version"]
    assert (
        "secret-that-must-not-be-logged"
        not in (state / "update-diagnostics/setup-report.md").read_text()
    )
