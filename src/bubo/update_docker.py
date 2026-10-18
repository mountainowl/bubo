"""External Docker controller primitives; no daemon socket enters the application."""

from __future__ import annotations

import copy
import http.client
import json
import os
import re
import socket
import stat
import time
import tomllib
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode

from bubo.update_identity import atomic_json
from bubo.update_installers import UnsafeInstallation, stable_version
from bubo.updater import Release

IMAGE = "ghcr.io/mountainowl/bubo"
_DIGEST = re.compile(re.escape(IMAGE) + r"@sha256:[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")


class DockerError(OSError):
    """Keep the status for recovery decisions without exposing daemon messages."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Docker request failed with status {status}")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


class DockerAPI:
    """Use only an explicitly selected local daemon socket and bounded responses."""

    def __init__(self, path: Path, *, timeout: float = 120):
        self.path = path.absolute()
        self.timeout = timeout

    def request(self, method: str, path: str, body=None, *, query=None, stream=False):
        if not stat.S_ISSOCK(self.path.stat().st_mode):
            raise UnsafeInstallation("A local Docker engine socket is required")
        connection = _UnixConnection(self.path, self.timeout)
        payload = json.dumps(body).encode() if body is not None else None
        try:
            connection.request(
                method,
                path + ("?" + urlencode(query) if query else ""),
                body=payload,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            if response.status >= 300:
                # Daemon messages may contain command arguments or environment data.
                raise DockerError(response.status)
            limit = 16 * 1024 * 1024
            content = response.read(limit + 1)
            if len(content) > limit:
                raise OSError("Docker response exceeded the size limit")
            if stream:
                for line in content.splitlines():
                    if line and json.loads(line).get("error"):
                        raise OSError("Docker image download failed")
                return None
            return json.loads(content) if content else None
        finally:
            connection.close()

    def inspect_container(self, identifier: str) -> dict:
        return self.request("GET", "/containers/" + quote(identifier, safe="") + "/json")

    def inspect_image(self, identifier: str) -> dict:
        return self.request("GET", "/images/" + quote(identifier, safe="") + "/json")


def resolve_release(api: DockerAPI, release: Release, *, architecture: str) -> Release:
    """Freeze the official version tag before any pull or maintenance operation.

    Docker's registry endpoint returns the immutable manifest/index digest. A
    multi-platform index remains pinned; staging verifies the selected image's
    actual platform and installed version before exposing persistent data.
    """
    stable_version(release.version)
    if not re.fullmatch(r"\d+\.\d+\.\d+", release.version):
        raise UnsafeInstallation("Docker requires an official stable version tag")
    metadata = api.request(
        "GET", "/distribution/" + quote(IMAGE + ":" + release.version, safe="") + "/json"
    )
    descriptor = metadata.get("Descriptor", {})
    digest = descriptor.get("digest", "")
    if (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
        or descriptor.get("mediaType")
        not in {
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        }
        or not any(
            platform.get("os") == "linux" and platform.get("architecture") == architecture
            for platform in metadata.get("Platforms", [])
        )
    ):
        raise UnsafeInstallation("Official Docker release digest or platform is unavailable")
    return Release(release.version, IMAGE + "@" + digest, digest.removeprefix("sha256:"))


def stage_image(
    api: DockerAPI, release: Release, destination: Path, *, architecture: str, timeout: float = 60
) -> dict:
    """Pull a pinned upstream image and check its installed version without user data.

    The probe's create intent survives a lost daemon response or controller crash.
    Its sole output is an exit status; daemon logs and application credentials are
    never copied into the retained transaction directory.
    The caller must hold the installation's attempt lock throughout staging.
    """
    if (
        not _DIGEST.fullmatch(release.artifact)
        or release.artifact.rsplit(":", 1)[1] != release.sha256
    ):
        raise UnsafeInstallation("A pinned upstream Docker image digest is required")

    def validate_image(image):
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", ""))
            or release.artifact not in image.get("RepoDigests", [])
            or image.get("Os") != "linux"
            or image.get("Architecture") != architecture
            or image.get("Config", {}).get("Volumes")
        ):
            raise UnsafeInstallation("Downloaded Docker image identity or platform does not match")

    record_path = destination / "docker-probe.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        if (
            not re.fullmatch(r"sha256:[0-9a-f]{64}", record.get("image", ""))
            or record.get("digest") != release.artifact
            or record.get("architecture") != architecture
            or record.get("version") != release.version
            or record.get("name") != "bubo-update-probe-" + record.get("token", "")
            or not re.fullmatch(r"[0-9a-f]{32}", record.get("token", ""))
        ):
            raise UnsafeInstallation("Docker probe recovery identity changed")
        image_id = record["image"]
    else:
        api.request(
            "POST",
            "/images/create",
            query={
                "fromImage": release.artifact,
                "platform": "linux/" + architecture,
            },
            stream=True,
        )
        image = api.inspect_image(release.artifact)
        validate_image(image)
        image_id = image["Id"]
        token = uuid.uuid4().hex
        record = {
            "image": image_id,
            "version": release.version,
            "name": "bubo-update-probe-" + token,
            "token": token,
            "digest": release.artifact,
            "architecture": architecture,
        }
        atomic_json(record_path, record)
    labels = {"io.bubo.update.probe": record["token"]}

    def owned():
        container = api.inspect_container(record.get("container_id", record["name"]))
        if (
            not _ID.fullmatch(container.get("Id", ""))
            or container.get("Image") != image_id
            or container.get("Config", {}).get("Labels", {}).get("io.bubo.update.probe")
            != record["token"]
            or container.get("Name") != "/" + record["name"]
            or (record.get("container_id") and container["Id"] != record["container_id"])
        ):
            raise UnsafeInstallation("Docker probe ownership is unavailable")
        return container

    if record.get("removing") and not record.get("removed"):
        try:
            owned()
        except DockerError as exc:
            if exc.status != 404:
                raise
            record["removed"] = True
            atomic_json(record_path, record)
    if not record.get("removed"):
        try:
            container = owned()
        except DockerError as exc:
            if exc.status != 404 or record.get("container_id"):
                raise
            api.request(
                "POST",
                "/containers/create",
                {
                    "Image": image_id,
                    "Entrypoint": ["python", "-c"],
                    "Cmd": [
                        "import sys; from importlib.metadata import version; "
                        "sys.exit(version('bubo') != sys.argv[1])",
                        release.version,
                    ],
                    "User": "10001:10001",
                    "Labels": labels,
                    "NetworkDisabled": True,
                    "HostConfig": {
                        "NetworkMode": "none",
                        "ReadonlyRootfs": True,
                        "CapDrop": ["ALL"],
                        "SecurityOpt": ["no-new-privileges:true"],
                        "RestartPolicy": {"Name": "no"},
                        "Memory": 134217728,
                        "PidsLimit": 32,
                    },
                },
                query={"name": record["name"]},
            )
            container = owned()
        record["container_id"] = container["Id"]
        atomic_json(record_path, record)
        identifier = record["container_id"]
        try:
            if "exit_code" not in record:
                state = container.get("State", {})
                if state.get("Status") == "created":
                    api.request("POST", "/containers/" + identifier + "/start")
                deadline = time.monotonic() + timeout
                while True:
                    state = owned().get("State", {})
                    if state.get("Status") == "exited" and state.get("Running") is False:
                        if not state.get("StartedAt") or state["StartedAt"].startswith("0001-"):
                            raise UnsafeInstallation("Docker version probe never started")
                        code = state.get("ExitCode")
                        if type(code) is not int:
                            raise UnsafeInstallation(
                                "Docker version probe exit status is unavailable"
                            )
                        record["exit_code"] = code
                        atomic_json(record_path, record)
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Docker version probe timed out")
                    time.sleep(0.1)
        finally:
            state = owned().get("State", {})
            if state.get("Running"):
                api.request("POST", "/containers/" + identifier + "/stop", query={"t": 10})
                state = owned().get("State", {})
            if state.get("Running") is not False:
                raise UnsafeInstallation("Docker version probe shutdown is unproven")
            # Persist removal intent first, so lost DELETE responses are recoverable.
            record["removing"] = True
            atomic_json(record_path, record)
            api.request("DELETE", "/containers/" + identifier)
            record["removed"] = True
            atomic_json(record_path, record)
    if record.get("exit_code") != 0:
        raise UnsafeInstallation("Docker image does not contain the requested Bubo version")
    # Recovery cleans the local probe without requiring registry connectivity.
    # Recheck the retained local image before publishing a usable staging receipt.
    image = api.inspect_image(image_id)
    validate_image(image)
    if image["Id"] != image_id:
        raise UnsafeInstallation("Docker staged image identity changed")
    package = {"version": release.version, "image_id": image_id, "digest": release.artifact}
    atomic_json(destination / "docker-image.json", package)
    return package


def replacement_preflight(
    api: DockerAPI, identifier: str, host_root: Path, *, retained_ids: frozenset[str] = frozenset()
) -> dict:
    """Check daemon state as well as the source's launch descriptor, before mutation.

    A descriptor cannot reveal customized writable layers or other containers
    that depend on this one. Never infer that a stopped, unknown container is
    coordinated: it could be restarted outside the maintenance protocol.
    """
    source = api.inspect_container(identifier)
    descriptor = launch_descriptor(source, api.inspect_image(source["Image"]), host_root)
    user = source["Config"].get("User", "").split(":", 1)[0]
    uid = int(user) if user.isdecimal() else {"": 0, "root": 0, "bubo": 10001}.get(user)
    if uid != os.geteuid():
        # Reading a mounted DB does not prove the application can read new
        # owner-only control records. Never chown user data or weaken permissions.
        raise UnsafeInstallation("External controller must run with the container runtime UID")
    info = api.request("GET", "/info")
    options = info.get("SecurityOptions") if isinstance(info, dict) else None
    if (
        not isinstance(options, list)
        or any(not isinstance(option, str) for option in options)
        or any(
            option.split(",", 1)[0] in {"name=userns", "name=rootless", "userns", "rootless"}
            for option in options
        )
        or info.get("Rootless")
        or source["HostConfig"].get("UsernsMode") not in {None, "", "host"}
    ):
        raise UnsafeInstallation("Remapped container identities require verified ownership mapping")
    changes = api.request("GET", "/containers/" + descriptor["container_id"] + "/changes")
    # Docker returns JSON null for an unchanged writable layer.
    if changes is not None and (not isinstance(changes, list) or changes):
        raise UnsafeInstallation("Container writable-layer changes require its owning updater")
    containers = api.request("GET", "/containers/json", query={"all": "true"})
    if not isinstance(containers, list):
        raise UnsafeInstallation("Container dependency inventory is unavailable")
    source_id, name = descriptor["container_id"], descriptor["name"]
    protected_mounts = [host_root.resolve()]
    for mount in source.get("Mounts", []):
        if mount.get("Type") not in {"bind", "volume"}:
            continue
        mount_source = mount.get("Source")
        if not isinstance(mount_source, str) or not Path(mount_source).is_absolute():
            raise UnsafeInstallation("Container mount source is unavailable")
        protected_mounts.append(Path(mount_source).resolve())

    def refers(value: str) -> bool:
        reference = value.removeprefix("container:").split(":", 1)[0].removeprefix("/")
        return bool(reference) and (reference == name or source_id.startswith(reference))

    for listed in containers:
        other_id = listed.get("Id")
        if not isinstance(other_id, str) or not _ID.fullmatch(other_id):
            raise UnsafeInstallation("Container dependency identity is unavailable")
        if other_id == source_id:
            continue
        other = api.inspect_container(other_id)
        if other.get("Id") != other_id or not isinstance(other.get("HostConfig"), dict):
            raise UnsafeInstallation("Container dependency inventory changed")
        host = other["HostConfig"]
        state = other.get("State", {})
        if (
            other_id in retained_ids
            and state.get("Running") is False
            and not any(state.get(key) for key in ("Restarting", "Paused", "RemovalInProgress"))
            and host.get("RestartPolicy", {}).get("Name") == "no"
        ):
            continue
        networks = other.get("NetworkSettings", {}).get("Networks")
        if not isinstance(networks, dict) or any(
            not isinstance(endpoint, dict) for endpoint in networks.values()
        ):
            raise UnsafeInstallation("Container network dependency inventory is unavailable")
        groups = [
            host.get("VolumesFrom"),
            host.get("Links"),
            *(endpoint.get("Links") for endpoint in networks.values()),
        ]
        if any(
            group is not None
            and (not isinstance(group, list) or any(not isinstance(item, str) for item in group))
            for group in groups
        ):
            raise UnsafeInstallation("Container dependency references are unavailable")
        references = [item for group in groups if group for item in group]
        references.extend(
            value
            for key in ("NetworkMode", "IpcMode", "PidMode")
            if isinstance(value := host.get(key), str) and value.startswith("container:")
        )
        if any(not isinstance(value, str) or refers(value) for value in references):
            raise UnsafeInstallation("Dependent containers require coordinated replacement")
        mounts = other.get("Mounts")
        if not isinstance(mounts, list):
            raise UnsafeInstallation("Container mount inventory is unavailable")
        for mount in mounts:
            source_path = mount.get("Source")
            if mount.get("Type") not in {"bind", "volume"}:
                continue
            if not isinstance(source_path, str) or not Path(source_path).is_absolute():
                raise UnsafeInstallation("Container mount source is unavailable")
            mounted = Path(source_path).resolve()
            if any(
                mounted.is_relative_to(root) or root.is_relative_to(mounted)
                for root in protected_mounts
            ):
                raise UnsafeInstallation("Shared persistent mounts require coordinated replacement")
    return descriptor


def launch_descriptor(container: dict, image: dict, host_root: Path) -> dict:
    """Validate ownership and preserve the complete Docker create configuration."""
    identifier = container.get("Id", "")
    if not _ID.fullmatch(identifier) or container.get("Image") != image.get("Id"):
        raise UnsafeInstallation("Container image identity is not stable")
    digests = [digest for digest in image.get("RepoDigests", []) if _DIGEST.fullmatch(digest)]
    config = copy.deepcopy(container["Config"])
    host_config = copy.deepcopy(container["HostConfig"])
    requested_image = config.get("Image", "")
    if (
        not digests
        or not (
            requested_image.startswith(IMAGE + ":")
            or _DIGEST.fullmatch(requested_image)
            or requested_image == image.get("Id") == container.get("Image")
        )
        or image.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.source")
        != "https://github.com/mountainowl/bubo"
    ):
        raise UnsafeInstallation(
            "Derivative or unverified Docker images require their owning updater"
        )
    if image.get("Os") != "linux" or image.get("Architecture") not in {"amd64", "arm64"}:
        raise UnsafeInstallation("Docker image platform is unsupported")
    labels = config.get("Labels") or {}
    if any(
        key.startswith(
            (
                "com.docker.compose.",
                "com.docker.swarm.",
                "io.kubernetes.",
                "io.k8s.",
                "com.hashicorp.nomad.",
            )
        )
        for key in labels
    ):
        raise UnsafeInstallation("Orchestrated containers must be updated by their owner")
    if host_config.get("AutoRemove"):
        raise UnsafeInstallation("Ephemeral containers have no safe resident replacement")
    if host_config.get("PidMode", "") not in {"", "private"}:
        raise UnsafeInstallation("Shared PID namespaces require coordinated replacement")
    if host_config.get("Privileged") or any(
        capability.upper().removeprefix("CAP_") in {"SYS_ADMIN", "ALL"}
        for capability in host_config.get("CapAdd") or []
    ):
        raise UnsafeInstallation(
            "Privileged namespace control requires operator-managed replacement"
        )
    state = container.get("State", {})
    if any(state.get(key) for key in ("Paused", "Restarting", "Dead", "RemovalInProgress")):
        raise UnsafeInstallation("Container must be stably running or stopped before updating")
    if host_config.get("VolumesFrom"):
        raise UnsafeInstallation("Inherited volumes require coordinated replacement")
    if any(
        str(host_config.get(key, "")).startswith("container:")
        for key in ("NetworkMode", "IpcMode", "PidMode")
    ):
        raise UnsafeInstallation("Shared container namespaces require coordinated replacement")
    environment = dict(value.split("=", 1) for value in config.get("Env", []) if "=" in value)
    if any(environment.get(key) for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")):
        raise UnsafeInstallation("Custom Python loading paths require operator-managed replacement")
    root = environment.get("BUBO_ROOT")
    if root is None:
        if config.get("User") not in {"bubo", "10001", "10001:10001"}:
            raise UnsafeInstallation("An explicit BUBO_ROOT is required for this container user")
        root = "/home/bubo/.local/share/bubo"
    container_root = PurePosixPath(root)
    if not container_root.is_absolute() or ".." in container_root.parts:
        raise UnsafeInstallation("Container state root is not an absolute persistent path")
    mounts = container.get("Mounts", [])
    matching = [mount for mount in mounts if mount.get("Destination") == str(container_root)]
    if (
        len(matching) != 1
        or matching[0].get("Type") not in {"bind", "volume"}
        or not matching[0].get("RW")
        or Path(matching[0].get("Source", "")).resolve() != host_root.resolve()
    ):
        raise UnsafeInstallation(
            "Bubo root must have one writable persistent host-accessible mount"
        )
    if any(container_root in PurePosixPath(mount["Destination"]).parents for mount in mounts):
        raise UnsafeInstallation("Nested Bubo state mounts need an explicit recovery mapping")
    # Retained overlays must not hide the upstream image's executables/imports.
    code_roots = (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/opt",
        "/app",
        "/home/bubo/.local/lib",
        "/home/bubo/.local/bin",
    )
    for mount in mounts:
        destination = PurePosixPath(mount.get("Destination", "/"))
        if any(
            destination.is_relative_to(path) or PurePosixPath(path).is_relative_to(destination)
            for path in ("/proc", "/sys")
        ):
            raise UnsafeInstallation(
                "Mounted process namespaces require operator-managed replacement"
            )
        if any(
            destination.is_relative_to(path) or PurePosixPath(path).is_relative_to(destination)
            for path in code_roots
        ):
            raise UnsafeInstallation(
                "Mounted application code requires operator-managed replacement"
            )
    config_path = host_root / "config" / "env.toml"
    if not config_path.is_file():
        raise UnsafeInstallation(
            "Persistent Bubo configuration and state are unavailable to the controller"
        )
    from bubo.env_config import expand_env_placeholders, section

    try:
        cfg = tomllib.loads(config_path.read_text())
        base = PurePosixPath(
            environment.get("BUBO_BASE_DIR")
            or expand_env_placeholders(
                str(section(cfg, "poller").get("state_dir", "var")), environment
            )
        )
        if not base.is_absolute():
            base = container_root / base
        if ".." in base.parts:
            raise ValueError("Parent path")
        relative_state = (base / "state").relative_to(container_root)
    except ValueError, KeyError:
        raise UnsafeInstallation(
            "Custom state needs an explicit persistent recovery mapping"
        ) from None
    host_state = host_root.joinpath(*relative_state.parts)
    if not host_state.is_dir() or not host_state.resolve().is_relative_to(host_root.resolve()):
        raise UnsafeInstallation("Persistent state is unavailable within the mapped root")
    # Config.Volumes alone allocates fresh anonymous volumes on create. Every
    # existing volume must instead have a reproducible source in HostConfig.
    explicit_mounts = {
        mount.get("Target", mount.get("Destination")): mount.get("Source")
        for mount in host_config.get("Mounts", [])
    }
    for bind in host_config.get("Binds") or []:
        pieces = bind.split(":")
        if len(pieces) in {2, 3}:
            explicit_mounts[pieces[1]] = pieces[0]
    for mount in mounts:
        if mount.get("Type") == "volume" and (
            not mount.get("Name") or explicit_mounts.get(mount["Destination"]) != mount["Name"]
        ):
            raise UnsafeInstallation("Anonymous or implicit volumes need an explicit mount mapping")
    # Randomly allocated ports would change clients' endpoints on replacement.
    # Defer rather than silently change launch semantics or guess a free port.
    if host_config.get("PublishAllPorts") or any(
        not binding.get("HostPort") or binding["HostPort"] == "0"
        for bindings in (host_config.get("PortBindings") or {}).values()
        for binding in (bindings or [])
    ):
        raise UnsafeInstallation("Dynamically allocated ports need explicit host port bindings")
    networks = {}
    for name, endpoint in container.get("NetworkSettings", {}).get("Networks", {}).items():
        networks[name] = {
            key: copy.deepcopy(endpoint[key])
            for key in ("IPAMConfig", "Links", "Aliases", "DriverOpts", "MacAddress", "GwPriority")
            if endpoint.get(key) is not None
        }
    # Docker-assigned endpoint IDs/IP addresses are omitted; explicit IPAM settings,
    # aliases, original ports/security/env/restart policy remain unchanged.
    return {
        "protocol_version": 1,
        "container_id": identifier,
        "name": container["Name"].removeprefix("/"),
        "image_id": image["Id"],
        "image_digest": digests[0],
        "architecture": image["Architecture"],
        "running": bool(container.get("State", {}).get("Running")),
        "container_root": str(container_root),
        "host_root": str(host_root.resolve()),
        "host_state": str(host_state.resolve()),
        "container_state": str(base / "state"),
        "create": {
            **config,
            "HostConfig": host_config,
            "NetworkingConfig": {"EndpointsConfig": networks},
        },
    }
