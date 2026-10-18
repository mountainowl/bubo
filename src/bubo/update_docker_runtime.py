"""Docker resident lifecycle for UpdateEngine; private launch state stays in Docker."""

import copy
import re
import tomllib
from pathlib import Path

from bubo.update_docker import DockerError
from bubo.update_docker_readiness import DockerReadiness
from bubo.update_identity import atomic_json
from bubo.update_installers import UnsafeInstallation
from bubo.update_readiness import _read


def resident_kind(specification: dict, root: Path) -> str:
    arguments = [*(specification.get("Entrypoint") or []), *(specification.get("Cmd") or [])]
    if not arguments:
        raise UnsafeInstallation("Docker installation has no resident command")
    command = Path(arguments[0]).name
    if (
        command in {"bubo-poller", "bubo"}
        and arguments[1:3] == ["service", "start"]
        and "--foreground" in arguments
    ):
        return "service"
    if command == "bubo-mcp":
        environment = dict(
            value.split("=", 1) for value in specification.get("Env", []) if "=" in value
        )
        config_path = root / "config/env.toml"
        config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
        transport = environment.get(
            "BUBO_MCP_TRANSPORT", config.get("mcp_server", {}).get("transport", "stdio")
        )
        if transport == "http":
            return "mcp-http"
        if transport == "stdio" and specification.get("OpenStdin"):
            return "mcp-stdio"
    raise UnsafeInstallation("Docker command requires an operator-managed resident lifecycle")


class DockerResidents:
    def __init__(self, installer, *, timeout: float = 40):
        self.installer, self.api, self.timeout = installer, installer.api, timeout

    def _path(self) -> Path:
        return self.installer.work() / "docker-candidate.json"

    def _owned(self, record: dict) -> dict | None:
        if self.api.request("GET", "/info").get("ID") != record["daemon_id"]:
            raise UnsafeInstallation("Docker candidate daemon identity changed")
        try:
            container = self.api.inspect_container(record.get("container_id", record["name"]))
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise
        policy = container.get("HostConfig", {}).get("RestartPolicy", {})
        allowed = [{"Name": "no"}, {"Name": "no", "MaximumRetryCount": 0}]
        if record.get("finalization_started"):
            allowed.append(record["restart_policy"])
        if (
            not re.fullmatch(r"[0-9a-f]{64}", container.get("Id", ""))
            or container.get("Image") != record["image"]
            or container.get("Name") != "/" + record["name"]
            or container.get("Config", {}).get("Labels", {}).get("io.bubo.update.request")
            != record["request_id"]
            or (record.get("container_id") and container["Id"] != record["container_id"])
            or policy not in allowed
        ):
            raise UnsafeInstallation("Docker candidate ownership changed")
        return container

    def quiesce(self, journal: dict) -> None:
        source = self.api.inspect_container(self.installer.container_id)
        resident_kind(source["Config"], self.installer.root)
        self.installer.source().retain()

    def stop(self, journal: dict) -> bool:
        path = self._path()
        if not path.exists():
            return True
        record = _read(path)
        if record["attempt_id"] != journal["attempt_id"] or record["token"] != journal["token"]:
            raise UnsafeInstallation("Docker candidate attempt changed")
        container = self._owned(record)
        if container is not None:
            record["container_id"] = container["Id"]
            atomic_json(path, record)
            identifier = container["Id"]
            if container["HostConfig"]["RestartPolicy"].get("Name") != "no":
                self.api.request(
                    "POST",
                    "/containers/" + identifier + "/update",
                    {
                        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    },
                )
            container = self._owned(record)
            if container is not None:
                if container["HostConfig"]["RestartPolicy"].get("Name") != "no":
                    return False
                if container["State"].get("Paused"):
                    return False
                if container["State"].get("Running") or container["State"].get("Restarting"):
                    self.api.request(
                        "POST",
                        "/containers/" + identifier + "/stop",
                        query={"t": record["stop_timeout"]},
                    )
                container = self._owned(record)
                if container is not None:
                    state = container.get("State", {})
                    if state.get("Running") is not False or state.get("Restarting"):
                        return False
                    self.api.request("DELETE", "/containers/" + identifier)
        record["removed"] = True
        atomic_json(path, record)
        return True

    def start(self, journal: dict, *, outcome: str) -> None:
        if not self.stop(journal) or not self.installer.writers_quiescent():
            raise UnsafeInstallation("Docker writers are not quiescent")
        source = self.installer.source()
        source.retain()  # Complete durable intent after an interrupted stop or rename.
        original = _read(source.path)
        specification = copy.deepcopy(source.create_spec())
        kind = resident_kind(specification, self.installer.root)
        image = self.installer.selected_image() if outcome == "committed" else original["image_id"]
        readiness = DockerReadiness(self.api, self.installer.state, kind, outcome=outcome)
        environment = dict(
            value.split("=", 1) for value in specification.get("Env", []) if "=" in value
        )
        specification["Env"] = [
            f"{key}={value}" for key, value in readiness.environment(environment).items()
        ]
        specification.setdefault("Labels", {})["io.bubo.update.request"] = readiness.request_id
        specification["Image"] = image
        daemon = self.api.request("GET", "/info").get("ID")
        if not daemon:
            raise UnsafeInstallation("Docker candidate daemon identity is unavailable")
        record = {
            "attempt_id": journal["attempt_id"],
            "token": journal["token"],
            "daemon_id": daemon,
            "name": original["name"],
            "image": image,
            "outcome": outcome,
            "request_id": readiness.request_id,
            "was_running": original["was_running"],
            "restart_policy": original["restart_policy"],
            "stop_timeout": specification.get("StopTimeout", 10),
        }
        atomic_json(self._path(), record)
        self.api.request(
            "POST", "/containers/create", specification, query={"name": record["name"]}
        )
        container = self._owned(record)
        if container is None:
            raise UnsafeInstallation("Docker candidate creation is unproven")
        record["container_id"] = container["Id"]
        atomic_json(self._path(), record)
        self.api.request("POST", "/containers/" + container["Id"] + "/start")
        readiness.bind(container["Id"], image)
        readiness.wait(timeout=self.timeout)

    def verify(self, journal: dict, *, outcome: str) -> bool:
        record = _read(self._path())
        if (
            record.get("removed")
            or record["outcome"] != outcome
            or record["attempt_id"] != journal["attempt_id"]
            or record["token"] != journal["token"]
            or self._owned(record) is None
        ):
            return False
        return DockerReadiness.recover(
            self.api, self.installer.state, record["request_id"]
        ).verify()

    def finalize(self, journal: dict, *, outcome: str) -> None:
        """Idempotent post-terminal publication; failure must never trigger rollback."""
        if journal["phase"] != outcome or outcome not in {"committed", "restored"}:
            raise UnsafeInstallation("Docker finalization requires a terminal decision")
        record = _read(self._path())
        if record["outcome"] != outcome or record["token"] != journal["token"]:
            raise UnsafeInstallation("Docker finalization attempt changed")
        container = self._owned(record)
        if container is None:
            raise UnsafeInstallation("Docker terminal candidate disappeared")
        if container.get("State", {}).get("Running"):
            if container["HostConfig"]["RestartPolicy"].get("Name") != "no":
                self.api.request(
                    "POST",
                    "/containers/" + record["container_id"] + "/update",
                    {
                        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    },
                )
            if not self.verify(journal, outcome=outcome):
                raise UnsafeInstallation("Docker terminal readiness is unproven")
        elif record["was_running"] or not record.get("finalization_started"):
            raise UnsafeInstallation("Docker terminal running state is unproven")
        if not record.get("finalization_started"):
            record["finalization_started"] = True
            atomic_json(self._path(), record)
        owner = _read(
            self.installer.installation / "residents" / (record["request_id"] + ".owner.json")
        )
        if container.get("State", {}).get("StartedAt") != owner["started_at"]:
            raise UnsafeInstallation("Docker terminal candidate restarted before finalization")
        identifier = record["container_id"]
        if not record["was_running"] and container["State"].get("Running"):
            self.api.request(
                "POST", "/containers/" + identifier + "/stop", query={"t": record["stop_timeout"]}
            )
        container = self._owned(record)
        if container is None or container["State"].get("Running") is not record["was_running"]:
            raise UnsafeInstallation("Docker terminal running state is unproven")
        source = _read(self.installer.source().path)
        retained_path = self.installer.installation / "docker-retained.json"
        retained = _read(retained_path) if retained_path.exists() else {"containers": []}
        if not any(item["id"] == source["container_id"] for item in retained["containers"]):
            retained["containers"].append(
                {
                    "id": source["container_id"],
                    "image": source["image_id"],
                    "name": source["retained_name"],
                }
            )
        atomic_json(retained_path, retained)
        for path in (
            self.installer.installation / "installation.json",
            self.installer.state / "update-registration.json",
        ):
            registration = _read(path)
            registration.setdefault("docker", {})["container_id"] = identifier
            if path.name == "installation.json":
                version_key = "target" if outcome == "committed" else "source"
                registration["source_version"] = journal[version_key]
            atomic_json(path, registration)
        self.api.request(
            "POST",
            "/containers/" + identifier + "/update",
            {
                "RestartPolicy": record["restart_policy"],
            },
        )
        container = self._owned(record)
        if (
            container is None
            or container["HostConfig"]["RestartPolicy"] != record["restart_policy"]
            or container["State"].get("StartedAt") != owner["started_at"]
            or container["State"].get("Running") is not record["was_running"]
            or container["State"].get("Paused")
            or container["State"].get("Restarting")
        ):
            raise UnsafeInstallation("Docker terminal restart policy is unproven")
        record["finalized"] = True
        atomic_json(self._path(), record)
