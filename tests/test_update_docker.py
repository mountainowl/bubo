from __future__ import annotations

import copy
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from bubo.update_docker import (
    IMAGE,
    DockerAPI,
    DockerError,
    launch_descriptor,
    replacement_preflight,
    resolve_release,
    stage_image,
)
from bubo.update_docker_installer import DockerInstaller
from bubo.update_docker_job import DockerJob
from bubo.update_docker_source import RetainedDockerSource
from bubo.update_identity import atomic_json
from bubo.update_installers import UnsafeInstallation
from bubo.updater import Release


@pytest.mark.parametrize("invalid", [None, "digest", "platform", "media", "version"])
def test_release_discovery_pins_official_version_and_platform(invalid):
    from types import SimpleNamespace

    metadata = {
        "Descriptor": {
            "digest": "sha256:" + "a" * 64,
            "mediaType": "application/vnd.oci.image.index.v1+json",
        },
        "Platforms": [{"os": "linux", "architecture": "arm64"}],
    }
    if invalid == "digest":
        metadata["Descriptor"]["digest"] = "latest"
    elif invalid == "platform":
        metadata["Platforms"][0]["architecture"] = "amd64"
    elif invalid == "media":
        metadata["Descriptor"]["mediaType"] = "application/json"

    def request(method, path):
        assert method == "GET"
        assert path == "/distribution/ghcr.io%2Fmountainowl%2Fbubo%3A0.28.0/json"
        return metadata

    api = SimpleNamespace(request=request)
    release = Release("0.28.0+local" if invalid == "version" else "0.28.0", "unused", "unused")
    if invalid:
        with pytest.raises(UnsafeInstallation):
            resolve_release(api, release, architecture="arm64")
    else:
        assert resolve_release(api, release, architecture="arm64") == Release(
            "0.28.0",
            IMAGE + "@sha256:" + "a" * 64,
            "a" * 64,
        )


@pytest.fixture
def docker_description(tmp_path):
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config/env.toml").write_text("auto_update = true\n")
    (root / "var/state").mkdir(parents=True)
    image = {
        "Id": "sha256:" + "a" * 64,
        "RepoDigests": [IMAGE + "@sha256:" + "b" * 64],
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "Labels": {"org.opencontainers.image.source": "https://github.com/mountainowl/bubo"}
        },
    }
    container = {
        "Id": "c" * 64,
        "Name": "/review-worker",
        "Image": image["Id"],
        "Config": {
            "Image": IMAGE + ":0.27.0",
            "User": "10001:10001",
            "Hostname": "review-worker",
            "Env": ["BUBO_ROOT=/data", "EXAMPLE_CREDENTIAL=keep-local"],
            "Entrypoint": ["bubo-poller"],
            "Cmd": ["service", "start", "--foreground"],
            "WorkingDir": "/home/bubo",
            "Labels": {"example.owner": "fixture"},
            "StopSignal": "SIGTERM",
            "StopTimeout": 30,
            "Healthcheck": {"Test": ["CMD", "bubo-poller", "health"]},
        },
        "HostConfig": {
            "AutoRemove": False,
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "NetworkMode": "review-net",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Memory": 536870912,
            "PortBindings": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8765"}]},
            "Binds": [str(root) + ":/data:rw"],
        },
        "Mounts": [{"Type": "bind", "Source": str(root), "Destination": "/data", "RW": True}],
        "State": {"Running": True},
        "NetworkSettings": {
            "Networks": {
                "review-net": {
                    "NetworkID": "runtime-network-id",
                    "EndpointID": "runtime-endpoint-id",
                    "IPAddress": "172.20.0.2",
                    "Aliases": ["reviews"],
                    "IPAMConfig": {"IPv4Address": "172.20.0.2"},
                }
            }
        },
    }
    return container, image, root


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
@pytest.mark.parametrize("mount_type", ["bind", "volume"])
def test_docker_descriptor_preserves_launch_semantics(docker_description, architecture, mount_type):
    container, image, root = docker_description
    image["Architecture"] = architecture
    container["Mounts"][0]["Type"] = mount_type
    if mount_type == "volume":
        container["Mounts"][0]["Name"] = "bubo-data"
        container["HostConfig"].pop("Binds")
        container["HostConfig"]["Mounts"] = [
            {
                "Type": "volume",
                "Source": "bubo-data",
                "Target": "/data",
                "ReadOnly": False,
                "VolumeOptions": {"NoCopy": True},
            }
        ]
    original = copy.deepcopy(container)
    result = launch_descriptor(container, image, root)
    for key, value in container["Config"].items():
        assert result["create"][key] == value
    assert result["create"]["HostConfig"] == container["HostConfig"]
    network = result["create"]["NetworkingConfig"]["EndpointsConfig"]["review-net"]
    assert network == {"Aliases": ["reviews"], "IPAMConfig": {"IPv4Address": "172.20.0.2"}}
    assert result["image_digest"].startswith(IMAGE + "@sha256:")
    assert container == original


@pytest.mark.parametrize(
    "case",
    [
        "derivative",
        "compose",
        "swarm",
        "kubernetes",
        "ephemeral",
        "missing_mount",
        "nested_mount",
        "readonly_mount",
        "shared_namespace",
    ],
)
def test_unsafe_docker_cases_defer_without_modification(docker_description, case):
    container, image, root = docker_description
    if case == "derivative":
        image["RepoDigests"] = ["example/agent-bundle@sha256:" + "d" * 64]
    elif case in {"compose", "swarm", "kubernetes"}:
        prefix = {
            "compose": "com.docker.compose",
            "swarm": "com.docker.swarm",
            "kubernetes": "io.kubernetes",
        }[case]
        container["Config"]["Labels"][prefix + ".owner"] = "fixture"
    elif case == "ephemeral":
        container["HostConfig"]["AutoRemove"] = True
    elif case == "missing_mount":
        container["Mounts"] = []
    elif case == "nested_mount":
        container["Mounts"].append({"Destination": "/data/state", "Type": "volume", "RW": True})
    elif case == "readonly_mount":
        container["Mounts"][0]["RW"] = False
    else:
        container["HostConfig"]["NetworkMode"] = "container:other"
    original = copy.deepcopy(container)
    with pytest.raises(UnsafeInstallation):
        launch_descriptor(container, image, root)
    assert container == original


def test_docker_api_round_trip_and_error_redaction():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            if self.path == "/failure":
                response, status = b'{"message":"EXAMPLE_CREDENTIAL=never-display"}', 500
            else:
                response, status = b'{"Id":"fixture"}', 201
            self.send_response(status)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory(prefix="bubo-docker-", dir="/tmp") as directory:
        path = Path(directory) / "engine.sock"
        with socketserver.UnixStreamServer(str(path), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                api = DockerAPI(path)
                assert api.request(
                    "POST",
                    "/containers/create",
                    {"Env": ["EXAMPLE_CREDENTIAL=local"]},
                    query={"name": "test worker"},
                ) == {"Id": "fixture"}
                assert requests[0] == (
                    "/containers/create?name=test+worker",
                    {"Env": ["EXAMPLE_CREDENTIAL=local"]},
                )
                with pytest.raises(OSError, match="status 500") as error:
                    api.request("POST", "/failure", {})
                assert "never-display" not in str(error.value)
            finally:
                server.shutdown()
                thread.join(timeout=2)


def test_docker_api_rejects_non_socket_paths(tmp_path):
    path = tmp_path / "not-a-socket"
    path.write_text("fixture")
    with pytest.raises(UnsafeInstallation, match="local Docker engine socket"):
        DockerAPI(path).request("GET", "/info")


class InventoryAPI:
    def __init__(self, source, image, others=(), changes=None):
        self.source = source
        self.image = image
        self.others = {item["Id"]: item for item in others}
        self.changes = changes or []
        self.security_options = []
        self.calls = []

    def inspect_container(self, identifier):
        return copy.deepcopy(
            self.source if identifier == self.source["Id"] else self.others[identifier]
        )

    def inspect_image(self, identifier):
        assert identifier == self.image["Id"]
        return copy.deepcopy(self.image)

    def request(self, method, path, *, query=None):
        self.calls.append((method, path, query))
        assert method == "GET", "Preflight must not mutate daemon state"
        if path == "/info":
            return {"SecurityOptions": self.security_options}
        if path.endswith("/changes"):
            return self.changes
        assert path == "/containers/json"
        assert query == {"all": "true"}
        return [{"Id": item} for item in (self.source["Id"], *self.others)]


@pytest.fixture
def preflight_description(docker_description):
    source, image, root = docker_description
    source["Config"]["User"] = str(os.geteuid())
    return source, image, root


def test_daemon_preflight_accepts_unmodified_independent_container(preflight_description):
    source, image, root = preflight_description
    api = InventoryAPI(source, image)
    assert replacement_preflight(api, source["Id"], root) == launch_descriptor(source, image, root)
    assert len(api.calls) == 3


def test_daemon_preflight_defers_custom_writable_layer_without_leaking_paths(preflight_description):
    source, image, root = preflight_description
    api = InventoryAPI(source, image, changes=[{"Path": "/private-secret-tool", "Kind": 1}])
    with pytest.raises(UnsafeInstallation, match="writable-layer") as error:
        replacement_preflight(api, source["Id"], root)
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize(
    "dependency",
    ["NetworkMode", "IpcMode", "PidMode", "VolumesFrom", "Links", "network-link", "mount"],
)
def test_daemon_preflight_defers_dependent_even_stopped_container(
    preflight_description, dependency
):
    source, image, root = preflight_description
    other = {
        "Id": "d" * 64,
        "HostConfig": {},
        "State": {"Running": False},
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
    }
    if dependency == "mount":
        other["Mounts"] = [{"Type": "bind", "Source": str(root), "RW": False}]
    elif dependency == "VolumesFrom":
        other["HostConfig"][dependency] = [source["Id"][:12] + ":ro"]
    elif dependency == "Links":
        other["HostConfig"][dependency] = [source["Name"] + ":/dependent/alias"]
    elif dependency == "network-link":
        other["NetworkSettings"]["Networks"] = {"private": {"Links": [source["Name"] + ":alias"]}}
    else:
        other["HostConfig"][dependency] = "container:" + source["Id"]
    api = InventoryAPI(source, image, [other])
    with pytest.raises(UnsafeInstallation, match="coordinated replacement"):
        replacement_preflight(api, source["Id"], root)


@pytest.mark.parametrize(
    ("running", "policy", "accepted"),
    [(False, "no", True), (True, "no", False), (False, "always", False)],
)
def test_only_known_stopped_nonrestarting_retained_container_is_exempt(
    preflight_description, running, policy, accepted
):
    source, image, root = preflight_description
    old = {
        "Id": "e" * 64,
        "HostConfig": {"RestartPolicy": {"Name": policy}},
        "State": {"Running": running},
        "Mounts": [{"Type": "bind", "Source": str(root)}],
        "NetworkSettings": {"Networks": {}},
    }
    api = InventoryAPI(source, image, [old])
    if accepted:
        assert replacement_preflight(api, source["Id"], root, retained_ids=frozenset({old["Id"]}))
    else:
        with pytest.raises(UnsafeInstallation, match="coordinated replacement"):
            replacement_preflight(api, source["Id"], root, retained_ids=frozenset({old["Id"]}))


def test_controller_uid_mismatch_defers_before_any_write(docker_description):
    source, image, root = docker_description
    source["Config"]["User"] = str(os.geteuid() + 1)
    api = InventoryAPI(source, image)
    with pytest.raises(UnsafeInstallation, match="runtime UID"):
        replacement_preflight(api, source["Id"], root)
    assert api.calls == []


@pytest.mark.parametrize("option", ["name=userns", "name=rootless"])
def test_matching_numeric_uid_does_not_accept_namespace_remapping(preflight_description, option):
    source, image, root = preflight_description
    api = InventoryAPI(source, image)
    api.security_options = [option]
    with pytest.raises(UnsafeInstallation, match="ownership mapping"):
        replacement_preflight(api, source["Id"], root)
    assert api.calls == [("GET", "/info", None)]


@pytest.mark.parametrize("flag", ["Paused", "Restarting", "Dead", "RemovalInProgress"])
def test_transitional_container_state_defers(docker_description, flag):
    container, image, root = docker_description
    container["State"][flag] = True
    with pytest.raises(UnsafeInstallation, match="stably"):
        launch_descriptor(container, image, root)


@pytest.mark.parametrize("port", ["", "0"])
def test_random_host_port_defers(docker_description, port):
    container, image, root = docker_description
    container["HostConfig"]["PortBindings"]["8765/tcp"][0]["HostPort"] = port
    with pytest.raises(UnsafeInstallation, match="ports"):
        launch_descriptor(container, image, root)


def test_implicit_volume_is_not_replaced_with_empty_volume(docker_description):
    container, image, root = docker_description
    container["Mounts"].append(
        {"Type": "volume", "Name": "anonymous-id", "Destination": "/work", "RW": True}
    )
    container["Config"]["Volumes"] = {"/work": {}}
    with pytest.raises(UnsafeInstallation, match="explicit mount"):
        launch_descriptor(container, image, root)


def test_inherited_volume_requires_coordinated_update(docker_description):
    container, image, root = docker_description
    container["HostConfig"]["VolumesFrom"] = ["other:rw"]
    with pytest.raises(UnsafeInstallation, match="Inherited"):
        launch_descriptor(container, image, root)


@pytest.mark.parametrize("base", [None, "custom", "/data/external"])
def test_docker_maps_actual_state_directory(docker_description, base):
    container, image, root = docker_description
    if base:
        (root / "config/env.toml").write_text(f'[poller]\nstate_dir = "{base}"\n')
    relative = (base or "var").removeprefix("/data/")
    (root / relative / "state").mkdir(parents=True, exist_ok=True)
    result = launch_descriptor(container, image, root)
    assert result["host_state"] == str((root / relative / "state").resolve())
    assert result["container_state"] == f"/data/{relative}/state"


def test_docker_state_environment_precedes_toml(docker_description):
    container, image, root = docker_description
    container["Config"]["Env"].append("BUBO_BASE_DIR=/data/override")
    (root / "override/state").mkdir(parents=True)
    result = launch_descriptor(container, image, root)
    assert result["host_state"] == str((root / "override/state").resolve())


@pytest.mark.parametrize("base", ["/unmounted", "../outside", "/data/../outside"])
def test_unmapped_docker_state_defers(docker_description, base):
    container, image, root = docker_description
    container["Config"]["Env"].append(f"BUBO_BASE_DIR={base}")
    with pytest.raises(UnsafeInstallation, match="mapping"):
        launch_descriptor(container, image, root)


@pytest.mark.parametrize("relation", ["same", "parent", "child"])
def test_shared_workspace_outside_bubo_root_defers(preflight_description, relation):
    source, image, root = preflight_description
    workspace = root.parent / "workspace"
    source["Mounts"].append(
        {"Type": "bind", "Source": str(workspace), "Destination": "/workspace", "RW": True}
    )
    source["HostConfig"]["Binds"].append(str(workspace) + ":/workspace:rw")
    shared = {"same": workspace, "parent": workspace.parent, "child": workspace / "repo"}[relation]
    other = {
        "Id": "d" * 64,
        "HostConfig": {},
        "State": {"Running": False},
        "Mounts": [{"Type": "bind", "Source": str(shared), "RW": False}],
        "NetworkSettings": {"Networks": {}},
    }
    with pytest.raises(UnsafeInstallation, match="Shared persistent"):
        replacement_preflight(InventoryAPI(source, image, [other]), source["Id"], root)


@pytest.mark.parametrize(
    "destination",
    ["/usr/local/lib/python3.14/site-packages", "/usr/local/bin", "/app", "/home/bubo"],
)
def test_mount_cannot_hide_replacement_code(docker_description, destination):
    source, image, root = docker_description
    source["Mounts"].append(
        {
            "Type": "bind",
            "Source": str(root.parent / "code"),
            "Destination": destination,
            "RW": False,
        }
    )
    with pytest.raises(UnsafeInstallation, match="Mounted application code"):
        launch_descriptor(source, image, root)


@pytest.mark.parametrize("key", ["PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE"])
def test_custom_import_environment_defers(docker_description, key):
    source, image, root = docker_description
    source["Config"]["Env"].append(key + "=/workspace")
    with pytest.raises(UnsafeInstallation, match="Python loading"):
        launch_descriptor(source, image, root)


def test_network_gateway_priority_preserved(docker_description):
    source, image, root = docker_description
    source["NetworkSettings"]["Networks"]["review-net"]["GwPriority"] = 10
    source["NetworkSettings"]["Networks"]["secondary"] = {"GwPriority": 0}
    descriptor = launch_descriptor(source, image, root)
    endpoints = descriptor["create"]["NetworkingConfig"]["EndpointsConfig"]
    assert endpoints["review-net"]["GwPriority"] == 10
    assert endpoints["secondary"]["GwPriority"] == 0


class ProbeAPI:
    """Daemon protocol fixture, not evidence of actual Docker execution."""

    def __init__(self, *, failure=None, exit_code=0):
        self.failure = failure
        self.exit_code = exit_code
        self.image = {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": [IMAGE + "@sha256:" + "b" * 64],
            "Os": "linux",
            "Architecture": "arm64",
        }
        self.container = None
        self.calls = []
        self.create_body = None

    def inspect_image(self, identifier):
        return copy.deepcopy(self.image)

    def inspect_container(self, identifier):
        if self.container is None:
            raise DockerError(404)
        assert identifier in {self.container["Id"], self.container["Name"].removeprefix("/")}
        return copy.deepcopy(self.container)

    def request(self, method, path, body=None, *, query=None, stream=False):
        self.calls.append((method, path))
        if path == "/images/create":
            assert stream
            assert query["platform"] == "linux/arm64"
            if self.failure == "offline":
                raise DockerError(503)
        elif path == "/containers/create":
            assert self.container is None
            self.create_body = body
            self.container = {
                "Id": "c" * 64,
                "Image": body["Image"],
                "Config": copy.deepcopy(body),
                "Name": "/" + query["name"],
                "State": {"Status": "created", "Running": False},
            }
            self.container["Config"]["Labels"] = {
                "org.opencontainers.image.source": "https://github.com/charitra/bubo",
                **body["Labels"],
            }
            if self.failure == "create":
                self.failure = None
                raise TimeoutError("lost create response")
        elif path.endswith("/start"):
            self.container["State"] = {
                "Status": "exited",
                "Running": False,
                "ExitCode": self.exit_code,
                "StartedAt": "2026-10-16T12:00:00Z",
            }
            if self.failure in {"timeout", "stop"}:
                self.container["State"].update(Status="running", Running=True)
        elif path.endswith("/stop"):
            if self.failure == "stop":
                raise DockerError(500)
            self.container["State"].update(Status="exited", Running=False, ExitCode=137)
        elif method == "DELETE":
            assert not self.container["State"]["Running"]
            self.container = None
            if self.failure == "delete":
                self.failure = None
                raise TimeoutError("lost delete response")
        else:
            raise AssertionError((method, path))


@pytest.fixture
def docker_release():
    return Release("0.28.0", IMAGE + "@sha256:" + "b" * 64, "b" * 64)


def test_stage_pinned_image_checks_version_without_user_data(tmp_path, docker_release):
    api = ProbeAPI()
    result = stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert result["image_id"] == api.image["Id"]
    assert json.loads((tmp_path / "docker-image.json").read_text()) == result
    assert api.container is None
    assert api.create_body["Cmd"][-1] == docker_release.version
    assert api.create_body["NetworkDisabled"] is True
    assert api.create_body["HostConfig"]["NetworkMode"] == "none"
    assert api.create_body["HostConfig"]["ReadonlyRootfs"] is True
    assert not api.create_body.get("Env")
    assert not api.create_body["HostConfig"].get("Mounts")
    assert not api.create_body["HostConfig"].get("Binds")


def test_stage_selects_source_platform_on_other_architecture_daemon(tmp_path, docker_release):
    api = ProbeAPI()
    request = api.request

    def native_amd64_daemon(method, path, body=None, *, query=None, stream=False):
        if path == "/images/create":
            api.image["Architecture"] = (query or {}).get("platform", "linux/amd64").split("/")[-1]
        return request(method, path, body, query=query, stream=stream)

    api.request = native_amd64_daemon
    result = stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert result["image_id"] == api.image["Id"]
    assert api.image["Architecture"] == "arm64"


@pytest.mark.parametrize("failure", ["create", "delete"])
def test_probe_recovers_lost_daemon_response_without_duplicate_create(
    tmp_path, docker_release, failure
):
    api = ProbeAPI(failure=failure)
    with pytest.raises(TimeoutError):
        stage_image(api, docker_release, tmp_path, architecture="arm64")
    stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert api.calls.count(("POST", "/containers/create")) == 1
    assert api.container is None


def test_probe_wrong_version_is_removed_but_never_staged(tmp_path, docker_release):
    api = ProbeAPI(exit_code=1)
    with pytest.raises(UnsafeInstallation, match="requested Bubo version"):
        stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert api.container is None
    assert not (tmp_path / "docker-image.json").exists()


@pytest.mark.parametrize("failure", ["timeout", "stop"])
def test_probe_timeout_requires_proven_stop_before_removal(tmp_path, docker_release, failure):
    api = ProbeAPI(failure=failure)
    with pytest.raises((TimeoutError, DockerError)):
        stage_image(api, docker_release, tmp_path, architecture="arm64", timeout=0)
    assert not (tmp_path / "docker-image.json").exists()
    if failure == "stop":
        assert api.container["State"]["Running"]
        assert not any(method == "DELETE" for method, _ in api.calls)
    else:
        assert api.container is None


def test_probe_recovery_does_not_touch_foreign_container(tmp_path, docker_release):
    api = ProbeAPI(failure="create")
    with pytest.raises(TimeoutError):
        stage_image(api, docker_release, tmp_path, architecture="arm64")
    api.container["Config"]["Labels"] = {}
    before = len(api.calls)
    with pytest.raises(UnsafeInstallation, match="ownership"):
        stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert api.calls[before:] == []


def test_existing_probe_recovers_without_registry_access(tmp_path, docker_release):
    api = ProbeAPI(failure="create")
    with pytest.raises(TimeoutError):
        stage_image(api, docker_release, tmp_path, architecture="arm64")
    api.failure = "offline"
    stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert api.calls.count(("POST", "/images/create")) == 1
    assert api.container is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("Architecture", "amd64"), ("RepoDigests", []), ("Config", {"Volumes": {"/data": {}}})],
)
def test_wrong_target_image_never_creates_probe(tmp_path, docker_release, field, value):
    api = ProbeAPI()
    api.image[field] = value
    with pytest.raises(UnsafeInstallation, match="identity or platform"):
        stage_image(api, docker_release, tmp_path, architecture="arm64")
    assert api.container is None


def test_unpinned_target_never_calls_daemon(tmp_path):
    api = ProbeAPI()
    with pytest.raises(UnsafeInstallation, match="pinned"):
        stage_image(
            api, Release("0.28.0", IMAGE + ":latest", "b" * 64), tmp_path, architecture="arm64"
        )
    assert api.calls == []


class SourceAPI(InventoryAPI):
    def __init__(self, *args, failure=None):
        super().__init__(*args)
        self.failure = failure
        self.daemon = {"ID": "local-daemon", "DefaultRuntime": "runc"}

    def request(self, method, path, body=None, *, query=None):
        if method == "GET":
            value = super().request(method, path, query=query)
            if path == "/info":
                value.update(self.daemon)
            return value
        self.calls.append((method, path, query))
        operation = path.rsplit("/", 1)[1]
        if operation == "update":
            self.source["HostConfig"]["RestartPolicy"] = copy.deepcopy(body["RestartPolicy"])
        elif operation == "stop":
            if self.failure != "unproven-stop":
                self.source["State"]["Running"] = False
        elif operation == "rename":
            self.source["Name"] = "/" + query["name"]
        else:
            raise AssertionError((method, path))
        if self.failure == operation:
            self.failure = None
            raise TimeoutError("lost mutation response")


def test_source_retention_keeps_private_launch_authority_in_daemon(preflight_description, tmp_path):
    source, image, root = preflight_description
    api = SourceAPI(source, image)
    retained = RetainedDockerSource(api, source["Id"], root, tmp_path)
    record = retained.retain()
    assert record["phase"] == "retained"
    assert record["was_running"] is True
    assert source["State"]["Running"] is False
    assert source["HostConfig"]["RestartPolicy"]["Name"] == "no"
    assert source["Name"] == "/" + record["retained_name"]
    assert ("POST", "/containers/" + source["Id"] + "/stop", {"t": 30}) in api.calls
    spec = retained.create_spec()
    assert "EXAMPLE_CREDENTIAL=keep-local" in spec["Env"]
    journal = retained.path.read_text()
    assert "keep-local" not in journal
    assert "EXAMPLE_CREDENTIAL" not in journal
    assert "Entrypoint" not in journal
    assert not any(method == "DELETE" or path.endswith("/start") for method, path, _ in api.calls)


@pytest.mark.parametrize("operation", ["update", "stop", "rename"])
def test_source_retention_recovers_lost_mutation_response(
    preflight_description, tmp_path, operation
):
    source, image, root = preflight_description
    api = SourceAPI(source, image, failure=operation)
    retained = RetainedDockerSource(api, source["Id"], root, tmp_path)
    with pytest.raises(TimeoutError):
        retained.retain()
    record = json.loads(retained.path.read_text())
    recovered = RetainedDockerSource(api, source["Id"], root, tmp_path)
    assert recovered.retain()["token"] == record["token"]
    assert (
        sum(path.endswith("/" + operation) for method, path, _ in api.calls if method == "POST")
        == 1
    )


def test_source_failed_stop_cannot_prove_backup_safe(preflight_description, tmp_path):
    source, image, root = preflight_description
    api = SourceAPI(source, image, failure="unproven-stop")
    retained = RetainedDockerSource(api, source["Id"], root, tmp_path)
    with pytest.raises(UnsafeInstallation, match="shutdown is unproven"):
        retained.retain()
    assert source["State"]["Running"] is True
    assert source["Name"] == "/review-worker"
    assert json.loads(retained.path.read_text())["phase"] == "intent"


@pytest.mark.parametrize("change", ["daemon", "runtime", "credentials", "restart"])
def test_retained_source_rejects_external_changes(preflight_description, tmp_path, change):
    source, image, root = preflight_description
    api = SourceAPI(source, image)
    retained = RetainedDockerSource(api, source["Id"], root, tmp_path)
    retained.retain()
    if change == "daemon":
        api.daemon["ID"] = "another-daemon"
    elif change == "runtime":
        api.daemon["DefaultRuntime"] = "new-runtime"
    elif change == "credentials":
        source["Config"]["Env"].append("SECRET=changed")
    else:
        source["HostConfig"]["RestartPolicy"] = {"Name": "always"}
    before = len(api.calls)
    with pytest.raises(UnsafeInstallation, match="changed"):
        retained.create_spec()
    assert all(method == "GET" for method, _, _ in api.calls[before:])


def test_source_durability_failure_prevents_first_mutation(
    preflight_description, tmp_path, monkeypatch
):
    source, image, root = preflight_description
    api = SourceAPI(source, image)

    def unwritable(*args):
        raise OSError("cannot persist intent")

    monkeypatch.setattr("bubo.update_docker_source.atomic_json", unwritable)
    with pytest.raises(OSError, match="cannot persist intent"):
        RetainedDockerSource(api, source["Id"], root, tmp_path).retain()
    assert all(method == "GET" for method, _, _ in api.calls)


def test_source_unbounded_shutdown_defers_before_mutation(preflight_description, tmp_path):
    source, image, root = preflight_description
    source["Config"]["StopTimeout"] = -1
    api = SourceAPI(source, image)
    with pytest.raises(UnsafeInstallation, match="bounded shutdown"):
        RetainedDockerSource(api, source["Id"], root, tmp_path).retain()
    assert all(method == "GET" for method, _, _ in api.calls)


def test_source_stop_retains_mac_when_daemon_clears_runtime_address(
    preflight_description, tmp_path, monkeypatch
):
    source, image, root = preflight_description
    endpoint = source["NetworkSettings"]["Networks"]["review-net"]
    endpoint["MacAddress"] = "02:42:ac:11:00:02"
    api = SourceAPI(source, image)
    request = api.request

    def clear_address(method, path, *args, **kwargs):
        result = request(method, path, *args, **kwargs)
        if path.endswith("/stop"):
            endpoint["MacAddress"] = ""
        return result

    monkeypatch.setattr(api, "request", clear_address)
    retained = RetainedDockerSource(api, source["Id"], root, tmp_path)
    retained.retain()
    spec = retained.create_spec()
    assert (
        spec["NetworkingConfig"]["EndpointsConfig"]["review-net"]["MacAddress"]
        == "02:42:ac:11:00:02"
    )
    assert endpoint["MacAddress"] == ""


def test_source_recovery_ignores_live_storage_usage(preflight_description, tmp_path):
    source, image, root = preflight_description
    api = SourceAPI(source, image, failure="stop")
    api.daemon["DriverStatus"] = [["Data Space Used", "1 GB"]]
    retained = RetainedDockerSource(api, source["Id"], root, tmp_path)
    with pytest.raises(TimeoutError):
        retained.retain()
    api.daemon["DriverStatus"] = [["Data Space Used", "2 GB"]]
    retained.retain()
    assert retained.create_spec()["Image"] == image["Id"]


def test_docker_source_is_available_in_retained_stdlib_archive(tmp_path):
    from bubo.update_helper import _bundle

    archive = _bundle(tmp_path, Path(sys._base_executable))
    result = subprocess.run(
        [
            sys._base_executable,
            "-I",
            "-S",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from bubo.update_docker import stage_image; "
            "from bubo.update_docker_source import RetainedDockerSource; "
            "from bubo.update_docker_readiness import DockerReadiness; "
            "from bubo.update_docker_installer import DockerInstaller",
            archive,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "configuration",
    [
        {"PidMode": "host"},
        {"PidMode": "container:other"},
        {"Privileged": True},
        {"CapAdd": ["SYS_ADMIN"]},
        {"CapAdd": ["CAP_SYS_ADMIN"]},
        {"CapAdd": ["ALL"]},
    ],
)
def test_namespace_escape_configuration_defers(docker_description, configuration):
    source, image, root = docker_description
    source["HostConfig"].update(configuration)
    with pytest.raises(UnsafeInstallation, match="namespace"):
        launch_descriptor(source, image, root)


@pytest.mark.parametrize("destination", ["/proc", "/proc/1", "/sys"])
def test_process_namespace_overlay_defers(docker_description, destination):
    source, image, root = docker_description
    source["Mounts"].append({"Type": "bind", "Source": "/proc", "Destination": destination})
    with pytest.raises(UnsafeInstallation, match="process namespaces"):
        launch_descriptor(source, image, root)


class JobAPI(ProbeAPI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.daemon_id = "same-daemon"

    def request(self, method, path, body=None, *, query=None, stream=False):
        if path == "/info":
            return {"ID": self.daemon_id}
        try:
            return super().request(method, path, body, query=query, stream=stream)
        finally:
            if path == "/containers/create" and self.container is not None:
                self.container["HostConfig"] = copy.deepcopy(body["HostConfig"])


@pytest.fixture
def docker_job(tmp_path):
    api = JobAPI()
    job = DockerJob(api, tmp_path / "writer.json")
    body = {
        "Image": api.image["Id"],
        "Entrypoint": ["python", "-c"],
        "Cmd": ["pass"],
        "Env": ["BUBO_MAINTENANCE_TOKEN=private-attempt-token"],
    }
    return api, job, body


def test_writer_exit_is_durable_and_credentials_are_not(docker_job):
    api, job, body = docker_job
    job.run(body)
    record = json.loads(job.path.read_text())
    assert record["exit_code"] == 0
    assert record["removed"] is True
    assert "private-attempt-token" not in job.path.read_text()
    assert api.container is None
    assert job.stop()
    job.run(body)
    assert api.calls.count(("POST", "/containers/create")) == 1


@pytest.mark.parametrize("operation", ["create", "delete"])
def test_writer_recovers_lost_create_and_delete_responses(docker_job, operation):
    api, job, body = docker_job
    api.failure = operation
    with pytest.raises(TimeoutError):
        job.run(body)
    job.run(body)
    assert api.container is None
    assert api.calls.count(("POST", "/containers/create")) == 1


def test_failed_writer_is_stopped_before_recovery_is_allowed(docker_job):
    api, job, body = docker_job
    api.failure = "stop"
    with pytest.raises(DockerError):
        job.run(body, timeout=0)
    assert api.container["State"]["Running"]
    with pytest.raises(DockerError):
        job.stop()
    api.failure = None
    assert job.stop()
    assert api.container is None
    with pytest.raises(UnsafeInstallation, match="validation failed"):
        job.run(body)


def test_writer_foreign_daemon_never_proves_quiescence(docker_job):
    api, job, body = docker_job
    api.failure = "stop"
    with pytest.raises(DockerError):
        job.run(body, timeout=0)
    api.daemon_id = "different-daemon"
    before = len(api.calls)
    with pytest.raises(UnsafeInstallation, match="daemon"):
        job.stop()
    assert len(api.calls) == before


def test_writer_rejects_changed_recovery_specification(docker_job):
    api, job, body = docker_job
    api.failure = "create"
    with pytest.raises(TimeoutError):
        job.run(body)
    body["Env"] = ["BUBO_MAINTENANCE_TOKEN=another-attempt"]
    with pytest.raises(UnsafeInstallation, match="specification changed"):
        job.run(body)
    assert job.stop()


def test_writer_never_adopts_unrelated_named_container(docker_job):
    api, job, body = docker_job
    api.failure = "create"
    with pytest.raises(TimeoutError):
        job.run(body)
    api.container["Config"]["Labels"].clear()
    before = len(api.calls)
    with pytest.raises(UnsafeInstallation, match="ownership"):
        job.stop()
    assert len(api.calls) == before


def test_writer_nonzero_exit_cannot_validate_migration(docker_job):
    api, job, body = docker_job
    api.exit_code = 1
    with pytest.raises(UnsafeInstallation, match="validation failed"):
        job.run(body)
    assert api.container is None
    assert job.stop()


def test_writer_reset_revalidates_freshly_restored_database(docker_job):
    api, job, body = docker_job
    job.run(body)
    old_token = json.loads(job.path.read_text())["token"]
    job.reset()
    assert not job.path.exists()
    assert json.loads(job.path.with_suffix(".previous.json").read_text())["token"] == old_token
    job.run(body)
    assert json.loads(job.path.read_text())["token"] != old_token
    assert api.calls.count(("POST", "/containers/create")) == 2


def test_writer_reset_refuses_to_forget_live_writer(docker_job):
    api, job, body = docker_job
    api.failure = "stop"
    with pytest.raises(DockerError):
        job.run(body, timeout=0)
    with pytest.raises(DockerError):
        job.reset()
    assert job.path.exists()
    assert not job.path.with_suffix(".previous.json").exists()


def test_installer_selects_target_and_recovers_retained_source(preflight_description, monkeypatch):
    source, original, root = preflight_description
    target = {
        **original,
        "Id": "sha256:" + "e" * 64,
        "RepoDigests": [IMAGE + "@sha256:" + "e" * 64],
    }

    class InstallerAPI(SourceAPI):
        def inspect_image(self, identifier):
            return copy.deepcopy(target if identifier == target["Id"] else original)

    api = InstallerAPI(source, original)
    state = root / "var/state"
    installation = root / ".updates/installation"
    work = installation / "attempts/test-attempt"
    state.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True)
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
    atomic_json(
        installation / "attempt.json",
        {
            "work": str(work),
            "states": [str(state)],
            "target": "0.28.0",
            "token": "maintenance",
        },
    )
    installer = DockerInstaller(api, state, source["Id"])
    with installer.maintenance():
        installer.source().retain()
        installer.snapshot(work)
        atomic_json(
            work / "docker-image.json",
            {
                "version": "0.28.0",
                "image_id": target["Id"],
                "digest": target["RepoDigests"][0],
            },
        )
        installer.activate(work)
        assert installer.selected_image() == target["Id"]
        seen = []
        monkeypatch.setattr(
            "bubo.update_docker_installer.migrate_state",
            lambda api, state, work, image, *, user: seen.append((image, user)),
        )
        installer.migrate_and_check([state], "maintenance")
        assert seen == [(target["Id"], source["Config"]["User"])]
        assert installer.writers_quiescent()
        assert installer.recovery_ready(work)
        installer.restore(work)
        assert installer.selected_image() == original["Id"]
        assert installer.verify_restored()
        # Mutation of the private recovery authority must block code recovery.
        source["Config"]["Env"].append("ADDED_AFTER_SNAPSHOT=1")
        assert not installer.verify_restored()


def test_daemon_preflight_accepts_null_empty_changes(preflight_description):
    source, image, root = preflight_description
    api = InventoryAPI(source, image)
    api.changes = None
    assert replacement_preflight(api, source["Id"], root)["container_id"] == source["Id"]
