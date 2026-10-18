"""Full engine/adapter protocol tests. These do not execute a Docker daemon."""

import copy
import json
import os
import sqlite3

import pytest

from bubo.analytics import AnalyticsConfig
from bubo.update_docker import IMAGE, DockerError
from bubo.update_docker_installer import DockerInstaller
from bubo.update_docker_runtime import DockerResidents
from bubo.update_identity import atomic_json
from bubo.updater import Release, UpdateEngine


class Daemon:
    def __init__(self, root, installation, *, failure=None, running=True):
        self.root, self.installation, self.failure = root, installation, failure
        self.images = {}
        for letter, version in (("a", "0.27.0"), ("b", "0.28.0"), ("c", "0.29.0")):
            self.images["sha256:" + letter * 64] = {
                "Id": "sha256:" + letter * 64,
                "RepoDigests": [IMAGE + "@sha256:" + letter * 64],
                "Os": "linux",
                "Architecture": "arm64",
                "version": version,
                "Config": {
                    "Labels": {
                        "org.opencontainers.image.source": "https://github.com/mountainowl/bubo"
                    }
                },
            }
        self.containers = {
            "1" * 64: {
                "Id": "1" * 64,
                "Image": "sha256:" + "a" * 64,
                "Name": "/bubo-test",
                "Config": {
                    "Image": IMAGE + ":v0.27.0",
                    "Entrypoint": None,
                    "Cmd": ["bubo-poller", "service", "start", "--foreground"],
                    "Env": ["BUBO_ROOT=/data/bubo", "EXAMPLE_CREDENTIAL=keep-private"],
                    "User": str(os.getuid()),
                    "Labels": {},
                    "StopTimeout": 30,
                },
                "HostConfig": {
                    "Binds": [str(root) + ":/data/bubo:rw"],
                    "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                    "NetworkMode": "review-net",
                },
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(root),
                        "Destination": "/data/bubo",
                        "RW": True,
                    }
                ],
                "NetworkSettings": {"Networks": {"review-net": {}}},
                "State": {
                    "Running": running,
                    "Status": "running" if running else "exited",
                    "StartedAt": "initial",
                },
            },
        }
        self.sequence = 1
        self.execs = {}
        self.events = []

    def inspect_container(self, identifier):
        for container in self.containers.values():
            if identifier in {container["Id"], container["Name"].removeprefix("/")}:
                return copy.deepcopy(container)
        raise DockerError(404)

    def inspect_image(self, identifier):
        for image in self.images.values():
            if identifier == image["Id"] or identifier in image["RepoDigests"]:
                return copy.deepcopy(image)
        raise DockerError(404)

    def _start(self, container):
        container["State"] = {
            "Running": True,
            "Status": "running",
            "StartedAt": str(self.sequence),
            "Pid": 12345,
        }
        self.sequence += 1
        labels = container["Config"].get("Labels", {})
        image = self.images[container["Image"]]
        if "io.bubo.update.request" in labels:
            request_id = labels["io.bubo.update.request"]
            path = self.installation / "residents" / (request_id + ".json")
            request = json.loads(path.read_text())
            loaded = (
                "wrong"
                if self.failure == "readiness" and image["version"] == "0.28.0"
                else image["version"]
            )
            atomic_json(
                path.with_suffix(".ready.json"),
                {
                    **request,
                    "loaded_version": loaded,
                    "pid": 7,
                    "process_identity": "proc:00000000-0000-0000-0000-000000000001:1234",
                },
            )
            path.with_suffix(".claim").touch()
            return
        if "io.bubo.update.job" in labels:
            assert (self.installation / "maintenance.json").exists()
            assert all(
                not item["State"]["Running"]
                for item in self.containers.values()
                if item["Id"] != container["Id"]
            )
            if image["version"] != "0.27.0":
                with sqlite3.connect(self.root / "var/state/reviewer.sqlite") as connection:
                    connection.execute("CREATE TABLE IF NOT EXISTS migrated (version TEXT)")
                    connection.execute("INSERT INTO migrated VALUES (?)", (image["version"],))
            self.events.append("migration:" + image["version"])
        container["State"].update(
            Running=False,
            Status="exited",
            ExitCode=1
            if self.failure == "migration"
            and image["version"] == "0.28.0"
            and "io.bubo.update.job" in labels
            else 0,
        )

    def request(self, method, path, body=None, *, query=None, stream=False):
        if path == "/info":
            return {
                "ID": "test-daemon",
                "SecurityOptions": ["name=seccomp"],
                "DefaultRuntime": "runc",
            }
        if path == "/images/create":
            return None
        if path.startswith("/distribution/"):
            assert path == "/distribution/ghcr.io%2Fmountainowl%2Fbubo%3A0.28.0/json"
            return {
                "Descriptor": {
                    "digest": "sha256:" + "b" * 64,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                },
                "Platforms": [{"os": "linux", "architecture": "arm64"}],
            }
        if path == "/containers/json":
            return [{"Id": identifier} for identifier in self.containers]
        if path == "/containers/create":
            if any(item["Name"] == "/" + query["name"] for item in self.containers.values()):
                raise DockerError(409)
            identifier = f"{self.sequence:064x}"
            self.sequence += 1
            mounts = []
            for bind in body["HostConfig"].get("Binds", []):
                source, destination, *_ = bind.split(":")
                mounts.append(
                    {"Type": "bind", "Source": source, "Destination": destination, "RW": True}
                )
            for mount in body["HostConfig"].get("Mounts", []):
                mounts.append(
                    {
                        **mount,
                        "Destination": mount["Target"],
                        "RW": not mount.get("ReadOnly", False),
                    }
                )
            self.containers[identifier] = {
                "Id": identifier,
                "Name": "/" + query["name"],
                "Image": body["Image"],
                "Config": copy.deepcopy(body),
                "HostConfig": copy.deepcopy(body["HostConfig"]),
                "Mounts": mounts,
                "State": {"Status": "created", "Running": False},
                "NetworkSettings": {
                    "Networks": copy.deepcopy(
                        body.get("NetworkingConfig", {}).get("EndpointsConfig", {})
                    )
                },
            }
            return {"Id": identifier}
        parts = path.strip("/").split("/")
        if parts[0] == "exec":
            if method == "GET":
                return self.execs[parts[1]]
            return None
        identifier = parts[1]
        container = self.containers[identifier]
        operation = parts[-1]
        if operation == "changes":
            return []
        if operation == "update":
            if self.failure == "finalize" and body["RestartPolicy"]["Name"] != "no":
                self.failure = None
                raise DockerError(500)
            container["HostConfig"]["RestartPolicy"] = copy.deepcopy(body["RestartPolicy"])
        elif operation == "rename":
            container["Name"] = "/" + query["name"]
        elif operation == "start":
            self._start(container)
        elif operation == "stop":
            if self.failure == "stop-candidate" and "io.bubo.update.request" in container[
                "Config"
            ].get("Labels", {}):
                raise DockerError(500)
            container["State"].update(Running=False, Status="exited", ExitCode=0)
        elif operation == "exec":
            execution = f"{self.sequence:064x}"
            self.sequence += 1
            self.execs[execution] = {
                "ID": execution,
                "ContainerID": identifier,
                "Running": False,
                "ExitCode": 0,
            }
            return {"Id": execution}
        elif method == "DELETE":
            assert not container["State"]["Running"]
            del self.containers[identifier]
        else:
            raise AssertionError((method, path))
        return None


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "root"
    state = root / "var/state"
    installation = root / ".updates/installation"
    state.mkdir(parents=True, exist_ok=True)
    installation.mkdir(parents=True)
    for name in ("attempt.lock", "registry.lock", "activity.lock"):
        (installation / name).touch()
    (state / "update-activity.lock").touch()
    (root / "config").mkdir(exist_ok=True)
    (root / "config/env.toml").write_text("auto_update=true\n[analytics]\nenabled=false\n")
    (state / "install_id").write_text("11111111-1111-4111-8111-111111111111")
    with sqlite3.connect(state / "reviewer.sqlite") as connection:
        connection.execute("CREATE TABLE user_data (value TEXT)")
        connection.execute("INSERT INTO user_data VALUES ('original')")
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
            "docker": {"container_id": "1" * 64},
        },
    )
    atomic_json(
        installation / "installation.json",
        {
            "kind": "docker",
            "states": [str(state)],
            "source_version": "0.27.0",
            "docker": {"container_id": "1" * 64},
        },
    )
    api = Daemon(root, installation)

    def engine():
        record = json.loads((installation / "installation.json").read_text())
        installer = DockerInstaller(api, state, record["docker"]["container_id"])
        return UpdateEngine(
            installation,
            [state],
            installer,
            source_version=record["source_version"],
            producer_version="0.28.0",
            enabled=lambda _: True,
            analytics=lambda _: AnalyticsConfig(enabled=False),
            residents=DockerResidents(installer, timeout=0.1),
        )

    return api, engine, state, installation


def release(letter="b", version="0.28.0"):
    return Release(version, IMAGE + "@sha256:" + letter * 64, letter * 64)


@pytest.mark.parametrize("running", [True, False])
def test_docker_engine_commits_and_preserves_original_running_state(runtime, running):
    api, engine, state, installation = runtime
    api.containers["1" * 64]["State"]["Running"] = running
    update = engine()
    result = update.run(release())
    assert result.status == "updated", result
    record = json.loads((installation / "installation.json").read_text())
    active = api.inspect_container(record["docker"]["container_id"])
    assert active["State"]["Running"] is running
    assert active["Image"] == "sha256:" + "b" * 64
    assert active["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    assert (state / "install_id").read_text() == "11111111-1111-4111-8111-111111111111"
    assert not update.fence.exists()
    for path in installation.rglob("*.json"):
        assert "keep-private" not in path.read_text()


@pytest.mark.parametrize("failure", ["migration", "readiness"])
def test_docker_engine_restores_code_database_and_resident(runtime, failure):
    api, engine, state, installation = runtime
    api.failure = failure
    update = engine()
    result = update.run(release())
    assert result.code == "restored", result
    with sqlite3.connect(state / "reviewer.sqlite") as connection:
        assert connection.execute("SELECT value FROM user_data").fetchall() == [("original",)]
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='migrated'").fetchall()
            == []
        )
    record = json.loads((installation / "installation.json").read_text())
    active = api.inspect_container(record["docker"]["container_id"])
    assert active["Image"] == "sha256:" + "a" * 64
    assert active["State"]["Running"]
    assert not update.fence.exists()


@pytest.mark.parametrize("running", [True, False])
def test_docker_terminal_finalization_recovers_without_rollback(runtime, running):
    api, engine, _, installation = runtime
    api.containers["1" * 64]["State"]["Running"] = running
    api.failure = "finalize"
    update = engine()
    assert update.run(release()).code == "finalization_failed"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    assert update.fence.exists()
    recovered = engine()
    assert recovered.recover().status == "updated"
    assert not recovered.fence.exists()
    assert api.events == ["migration:0.28.0"]
    assert (
        json.loads((installation / "installation.json").read_text())["source_version"] == "0.28.0"
    )


@pytest.mark.parametrize("operation", ["stop", "rename"])
def test_docker_recovers_interrupted_source_retention(runtime, monkeypatch, operation):
    api, engine, state, installation = runtime
    request = api.request

    def interrupted(method, path, *args, **kwargs):
        result = request(method, path, *args, **kwargs)
        if path == "/containers/" + "1" * 64 + "/" + operation:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(api, "request", interrupted)
    update = engine()
    with pytest.raises(KeyboardInterrupt):
        update.run(release())
    journal = json.loads(update.journal_path.read_text())
    assert journal["phase"] == "quiescing"
    monkeypatch.setattr(api, "request", request)
    result = engine().recover()
    assert result.code == "restored", result
    assert not update.fence.exists()
    record = json.loads((installation / "installation.json").read_text())
    active = api.inspect_container(record["docker"]["container_id"])
    assert active["Image"] == "sha256:" + "a" * 64
    assert active["State"]["Running"]
    with sqlite3.connect(state / "reviewer.sqlite") as connection:
        assert connection.execute("SELECT value FROM user_data").fetchall() == [("original",)]


def test_second_docker_update_accepts_only_owned_stopped_history(runtime):
    api, engine, _, installation = runtime
    assert engine().run(release()).status == "updated"
    result = engine().run(release("c", "0.29.0"))
    assert result.status == "updated", result
    assert (
        json.loads((installation / "installation.json").read_text())["source_version"] == "0.29.0"
    )
    assert api.events == ["migration:0.28.0", "migration:0.29.0"]


def test_reconfigure_after_committed_update_validates_retained_history(runtime, monkeypatch):
    from bubo import update_docker_setup, update_helper

    api, engine, state, installation = runtime
    record = json.loads((installation / "installation.json").read_text())
    record.update(protocol_version=1, owner=os.getuid())
    socket = installation / "fake.sock"
    record["docker"].update(socket=str(socket), daemon_id=api.request("GET", "/info")["ID"])
    atomic_json(installation / "installation.json", record)
    assert engine().run(release()).status == "updated"
    active = json.loads((installation / "installation.json").read_text())["docker"]["container_id"]
    monkeypatch.setattr(update_docker_setup, "DockerAPI", lambda path: api)
    monkeypatch.setattr(update_docker_setup, "probe_storage", lambda *args, **kwargs: "0.28.0")
    monkeypatch.setattr(update_helper, "register_schedule", lambda *args, **kwargs: "fake")
    record = update_docker_setup.configure(api.root, active, socket)
    assert record["ready"] is True
    assert record["docker"]["container_id"] == active
    assert record["source_version"] == "0.28.0"
    assert (state / "install_id").read_text() == "11111111-1111-4111-8111-111111111111"
    api.containers["1" * 64]["State"]["Running"] = True
    from bubo.update_installers import UnsafeInstallation

    with pytest.raises(UnsafeInstallation, match="ownership changed"):
        update_docker_setup.configure(api.root, active, socket)


def test_host_setup_probe_schedule_and_helper_update_together(runtime, monkeypatch, tmp_path):
    import subprocess
    import sys
    from types import SimpleNamespace

    from bubo import update_docker, update_docker_setup, update_helper

    api, _, state, installation = runtime
    (installation / "installation.json").unlink()
    (state / "update-registration.json").unlink()
    request = api.request
    commands = {}

    def exec_transport(method, path, body=None, **kwargs):
        result = request(method, path, body, **kwargs)
        if path.endswith("/exec") and body["Cmd"][2] == update_docker_setup._PROBE:
            command = [sys.executable, *body["Cmd"][1:]]
            command[3] = str(api.root / command[3].removeprefix("/data/bubo/"))
            commands[result["Id"]] = command
        if path.startswith("/exec/") and path.endswith("/start"):
            execution = path.split("/")[2]
            if execution in commands:
                api.execs[execution]["ExitCode"] = subprocess.run(
                    commands[execution],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                ).returncode
        return result

    monkeypatch.setattr(api, "request", exec_transport)
    monkeypatch.setattr(update_docker_setup, "DockerAPI", lambda path: api)
    monkeypatch.setattr(update_docker, "DockerAPI", lambda path: api)
    monkeypatch.setattr(update_helper, "latest_release", lambda version: release())
    scheduled = []

    def scheduler(command, **kwargs):
        scheduled.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    record = update_docker_setup.configure(
        api.root,
        "1" * 64,
        installation / "fake.sock",
        home=tmp_path / "scheduler-home",
        runner=scheduler,
    )
    assert record["ready"] is True
    assert scheduled
    assert len(commands) == 2
    assert update_helper.tick(installation).status == "updated"
    assert api.events == ["migration:0.28.0"]


@pytest.mark.parametrize(
    "failure", [None, "lost-delete", "modified", "running", "fenced", "missing-newest"]
)
def test_retained_container_cleanup_is_bounded_owned_and_retryable(runtime, monkeypatch, failure):
    from bubo.update_installers import UnsafeInstallation

    api, engine, state, installation = runtime
    assert engine().run(release()).status == "updated"
    assert engine().run(release("c", "0.29.0")).status == "updated"
    active = json.loads((installation / "installation.json").read_text())["docker"]["container_id"]
    installer = DockerInstaller(api, state, active)
    path = installation / "docker-retained.json"
    records = json.loads(path.read_text())["containers"]
    assert len(records) == 2
    if failure == "missing-newest":
        del api.containers[records[-1]["id"]]
    request = api.request

    def interrupted(method, url, *args, **kwargs):
        if method == "DELETE":
            assert url == "/containers/" + "1" * 64
            assert kwargs["query"] == {"force": "false", "v": "false"}
        if failure == "modified" and url == "/containers/" + "1" * 64 + "/changes":
            return [{"Path": "/operator-note", "Kind": 1}]
        result = request(method, url, *args, **kwargs)
        if failure == "lost-delete" and method == "DELETE":
            raise DockerError(500)
        return result

    monkeypatch.setattr(api, "request", interrupted)
    if failure == "running":
        api.containers["1" * 64]["State"]["Running"] = True
    if failure == "fenced":
        (installation / "maintenance.json").write_text("{}")
    if failure in {"lost-delete", "modified", "running"}:
        with pytest.raises((DockerError, UnsafeInstallation)):
            installer.cleanup()
    else:
        installer.cleanup()
    if failure == "lost-delete":
        monkeypatch.setattr(api, "request", request)
        installer.cleanup()
    if failure in {None, "lost-delete"}:
        assert "1" * 64 not in api.containers
        assert json.loads(path.read_text())["containers"] == records[-1:]
        installer.cleanup()
    elif failure == "missing-newest":
        assert "1" * 64 in api.containers
        assert json.loads(path.read_text())["containers"] == records[:1]
    else:
        assert "1" * 64 in api.containers
        assert json.loads(path.read_text())["containers"] == records
    assert active in api.containers
    if failure != "missing-newest":
        assert records[-1]["id"] in api.containers


@pytest.mark.parametrize("operation", ["create", "start"])
def test_docker_recovers_candidate_after_lost_response(runtime, monkeypatch, operation):
    api, engine, state, installation = runtime
    request = api.request

    def interrupted(method, path, body=None, **kwargs):
        result = request(method, path, body, **kwargs)
        if operation == "create" and path == "/containers/create":
            candidate = "io.bubo.update.request" in body.get("Labels", {})
        elif operation == "start" and path.endswith("/start"):
            candidate = "io.bubo.update.request" in api.containers[path.split("/")[2]][
                "Config"
            ].get("Labels", {})
        else:
            candidate = False
        if candidate:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(api, "request", interrupted)
    update = engine()
    with pytest.raises(KeyboardInterrupt):
        update.run(release())
    assert update.fence.exists()
    monkeypatch.setattr(api, "request", request)
    result = engine().recover()
    assert result.code == "restored", result
    assert not update.fence.exists()
    with sqlite3.connect(state / "reviewer.sqlite") as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='migrated'"
        ).fetchall()
    record = json.loads((installation / "installation.json").read_text())
    assert api.inspect_container(record["docker"]["container_id"])["Image"] == "sha256:" + "a" * 64


def test_docker_failed_stop_blocks_database_restore(runtime, monkeypatch):
    api, engine, state, _ = runtime
    api.failure = "readiness"
    request = api.request

    def cannot_stop(method, path, *args, **kwargs):
        if path.endswith("/stop"):
            container = api.containers[path.split("/")[2]]
            if "io.bubo.update.request" in container["Config"].get("Labels", {}):
                raise DockerError(500)
        return request(method, path, *args, **kwargs)

    monkeypatch.setattr(api, "request", cannot_stop)
    update = engine()
    result = update.run(release())
    assert result.status == "failed", result
    assert update.fence.exists()
    with sqlite3.connect(state / "reviewer.sqlite") as connection:
        assert connection.execute("SELECT version FROM migrated").fetchall() == [("0.28.0",)]
    monkeypatch.setattr(api, "request", request)
    assert engine().recover().code == "restored"
    assert not update.fence.exists()


@pytest.mark.parametrize("change", ["running", "image", "name", "restart"])
def test_docker_changed_retained_source_blocks_next_update(runtime, change):
    api, engine, _, _ = runtime
    assert engine().run(release()).status == "updated"
    source = api.containers["1" * 64]
    if change == "running":
        source["State"]["Running"] = True
    elif change == "image":
        source["Image"] = "sha256:" + "c" * 64
    elif change == "name":
        source["Name"] = "/operator-owned"
    else:
        source["HostConfig"]["RestartPolicy"]["Name"] = "always"
    update = engine()
    result = update.run(release("c", "0.29.0"))
    assert result.status != "updated"
    assert not update.fence.exists()
    assert api.events == ["migration:0.28.0"]


@pytest.mark.parametrize("automatic", [True, False])
@pytest.mark.parametrize("failure", [None, "finalize", "cleanup"])
def test_retained_helper_dispatches_docker_transaction(runtime, monkeypatch, automatic, failure):
    from bubo import update_docker, update_helper

    api, _, _state, installation = runtime
    record = json.loads((installation / "installation.json").read_text())
    record.update(protocol_version=1, owner=os.getuid(), helper_version="0.28.0", next_check=0)
    record["docker"]["socket"] = str(installation / "fake.sock")
    atomic_json(installation / "installation.json", record)
    monkeypatch.setattr(update_docker, "DockerAPI", lambda path: api)
    monkeypatch.setattr(update_helper, "latest_release", lambda version: release())
    api.failure = failure
    if failure == "cleanup":
        from bubo.update_reporting import FailureStore

        def unavailable(*args, **kwargs):
            raise OSError("unavailable")

        monkeypatch.setattr(DockerInstaller, "cleanup", unavailable)
        monkeypatch.setattr(FailureStore, "record", unavailable)
    if not automatic:
        (api.root / "config/env.toml").write_text("auto_update=false\n[analytics]\nenabled=false\n")
        assert update_helper.tick(installation).code == "not_due"
        assert api.events == []
    result = update_helper.tick(installation, trigger="automatic" if automatic else "manual")
    if failure == "finalize":
        assert result.code == "finalization_failed"

        def no_discovery(version):
            pytest.fail("Recovery must not depend on registry discovery")

        monkeypatch.setattr(update_helper, "latest_release", no_discovery)
        result = update_helper.tick(installation)
    assert result.status == "updated", result
    updated = json.loads((installation / "installation.json").read_text())
    assert updated["docker"]["container_id"] != "1" * 64
    assert updated["source_version"] == "0.28.0"
    assert api.events == ["migration:0.28.0"]
