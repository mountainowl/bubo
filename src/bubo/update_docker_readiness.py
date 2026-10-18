"""Bind startup receipts to a live daemon container, never to a host PID."""

from __future__ import annotations

import re
import time
import uuid
from pathlib import Path

from bubo.update_docker import DockerAPI
from bubo.update_identity import atomic_json
from bubo.update_readiness import ResidentReadiness, StartupRejected, _read, _release
from bubo.update_registration import load_registration

_ID = re.compile(r"[0-9a-f]{64}")
_PROCESS_CHECK = (
    "import pathlib, sys; "
    "fields=pathlib.Path('/proc/'+sys.argv[1]+'/stat').read_text().rsplit(')',1)[1].split(); "
    "boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(); "
    "sys.exit(fields[0] in ('Z','X','x') or ('proc:'+boot+':'+fields[19]) != sys.argv[2])"
)


class DockerReadiness(ResidentReadiness):
    def __init__(self, api: DockerAPI, state: Path, kind: str, *, outcome: str = "committed"):
        self.api = api
        self.owner: dict | None = None
        self._require_host(state)
        super().__init__(state, kind, outcome=outcome)

    @staticmethod
    def _require_host(state: Path) -> dict:
        registration = load_registration(state)
        if registration.get("external") != "docker" or registration.get("view") != "host":
            raise StartupRejected("Docker readiness requires the external host controller")
        return registration

    @classmethod
    def recover(cls, api: DockerAPI, state: Path, request_id: str) -> DockerReadiness:
        """Reattach a retained daemon-owned candidate after controller interruption."""
        if str(uuid.UUID(request_id)) != request_id:
            raise StartupRejected("Invalid Docker startup request")
        self = cls.__new__(cls)
        self.api, self.state, self.request_id = api, state.resolve(), request_id
        self.installation = Path(self._require_host(state)["installation"])
        self.path = self.installation / "residents" / f"{request_id}.json"
        self.ready_path = self.path.with_suffix(".ready.json")
        self.owner_path = self.path.with_suffix(".owner.json")
        self.request = _read(self.path)
        self.owner = _read(self.owner_path)
        if (
            self.request.get("protocol_version") != 1
            or self.request.get("state") != str(self.state)
            or self.request.get("kind") not in {"service", "mcp-http", "mcp-stdio"}
            or self.request.get("outcome") not in {"committed", "restored"}
            or any(self.owner.get(key) != value for key, value in self.request.items())
        ):
            raise StartupRejected("Docker startup recovery identity changed")
        self._validate_attempt()
        self._inspect()
        return self

    def bind(self, identifier: str, image: str) -> None:
        """Bind the exact created container after start, using daemon identity."""
        if self.owner is not None or not _ID.fullmatch(identifier):
            raise StartupRejected("Docker candidate is already bound or invalid")
        self._validate_attempt()
        daemon = self.api.request("GET", "/info").get("ID")
        container = self.api.inspect_container(identifier)
        state = container.get("State", {})
        started = state.get("StartedAt")
        if not daemon or not isinstance(started, str) or not started or started.startswith("0001-"):
            raise StartupRejected("Docker candidate start identity is unavailable")
        self.owner = {
            **self.request,
            "daemon_id": daemon,
            "container_id": identifier,
            "image_id": image,
            "started_at": started,
        }
        self._inspect()
        atomic_json(self.owner_path, self.owner)

    def _inspect(self) -> None:
        owner = self.owner
        if owner is None:
            raise StartupRejected("Docker candidate ownership is unavailable")
        if (
            not _ID.fullmatch(owner.get("container_id", ""))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", owner.get("image_id", ""))
            or self.api.request("GET", "/info").get("ID") != owner.get("daemon_id")
        ):
            raise StartupRejected("Docker candidate daemon or image identity changed")
        container = self.api.inspect_container(owner["container_id"])
        state = container.get("State", {})
        if (
            container.get("Id") != owner["container_id"]
            or container.get("Image") != owner["image_id"]
            or state.get("StartedAt") != owner["started_at"]
            or state.get("Running") is not True
            or state.get("Paused")
            or state.get("Restarting")
            or state.get("Dead")
            or container.get("Config", {}).get("Labels", {}).get("io.bubo.update.request")
            != self.request_id
            or container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"
            or container.get("HostConfig", {}).get("PidMode", "") not in {"", "private"}
        ):
            raise StartupRejected("Docker candidate is no longer the launched container")

    def _prove_process(self, pid: int, identity: str) -> None:
        if not re.fullmatch(r"proc:[0-9a-f-]{36}:[0-9]+", identity):
            raise StartupRejected("Docker resident process identity is invalid")
        identifier = self.owner["container_id"]
        execution = self.api.request(
            "POST",
            "/containers/" + identifier + "/exec",
            {
                "Cmd": ["python", "-c", _PROCESS_CHECK, str(pid), identity],
                "AttachStdout": False,
                "AttachStderr": False,
                "AttachStdin": False,
            },
        ).get("Id", "")
        if not _ID.fullmatch(execution):
            raise StartupRejected("Docker namespace verification could not start")
        self.api.request("POST", "/exec/" + execution + "/start", {"Detach": True, "Tty": False})
        deadline = time.monotonic() + 3
        while True:
            result = self.api.request("GET", "/exec/" + execution + "/json")
            if result.get("ContainerID") != identifier or result.get("ID") != execution:
                raise StartupRejected("Docker namespace verification identity changed")
            if result.get("Running") is False:
                if type(result.get("ExitCode")) is not int or result["ExitCode"] != 0:
                    raise StartupRejected("Docker receipt does not match its namespace process")
                return
            if time.monotonic() >= deadline:
                raise StartupRejected("Docker namespace verification timed out")
            time.sleep(0.05)

    def verify(self) -> bool:
        self._validate_attempt()
        self._inspect()
        if _read(self.path) != self.request or _read(self.owner_path) != self.owner:
            raise StartupRejected("Docker candidate ownership record changed")
        if not self.ready_path.exists():
            return False
        receipt = _read(self.ready_path)
        loaded = receipt.get("loaded_version")
        if (
            any(receipt.get(key) != value for key, value in self.request.items())
            or type(receipt.get("pid")) is not int
            or receipt["pid"] <= 0
            or not isinstance(receipt.get("process_identity"), str)
            or not isinstance(loaded, str)
            or _release(loaded) != _release(self.request["version"])
            or not self.path.with_suffix(".claim").is_file()
        ):
            raise StartupRejected("Docker readiness does not match the candidate")
        self._prove_process(receipt["pid"], receipt["process_identity"])
        # A restart during the exec probe invalidates the receipt even if a PID
        # was reused inside the container. Never inspect daemon State.Pid locally.
        self._inspect()
        self._validate_attempt()
        return True
