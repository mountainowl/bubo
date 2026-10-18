"""Journal a short-lived Docker writer so recovery can prove it stopped."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from bubo.update_docker import DockerAPI, DockerError
from bubo.update_identity import atomic_json, sync_directory
from bubo.update_installers import UnsafeInstallation


class DockerJob:
    """Caller owns the attempt lock and maintenance fence for the entire job."""

    def __init__(self, api: DockerAPI, path: Path):
        self.api, self.path = api, path

    def _read(self) -> dict:
        record = json.loads(self.path.read_text())
        if (
            record.get("protocol_version") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", record.get("token", ""))
            or record.get("name") != "bubo-update-job-" + record["token"]
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", record.get("image", ""))
        ):
            raise UnsafeInstallation("Docker writer recovery record is invalid")
        return record

    def _inspect(self, record: dict) -> dict | None:
        if self.api.request("GET", "/info").get("ID") != record.get("daemon_id"):
            raise UnsafeInstallation("Docker writer daemon identity changed")
        try:
            container = self.api.inspect_container(record.get("container_id", record["name"]))
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise
        if (
            not re.fullmatch(r"[0-9a-f]{64}", container.get("Id", ""))
            or container.get("Image") != record["image"]
            or container.get("Name") != "/" + record["name"]
            or container.get("Config", {}).get("Labels", {}).get("io.bubo.update.job")
            != record["token"]
            or (record.get("container_id") and container["Id"] != record["container_id"])
            or container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"
        ):
            raise UnsafeInstallation("Docker writer ownership is unavailable")
        return container

    def stop(self) -> bool:
        """True means no job writer can survive; errors never authorize DB restore."""
        if not self.path.exists():
            return True
        record = self._read()
        container = self._inspect(record)
        if container is not None:
            identifier = container["Id"]
            # Persist a recovered create before attempting any destructive action.
            record["container_id"] = identifier
            atomic_json(self.path, record)
            state = container.get("State", {})
            if state.get("Paused"):
                raise UnsafeInstallation("Paused Docker writer requires operator recovery")
            if state.get("Running") or state.get("Restarting"):
                self.api.request("POST", "/containers/" + identifier + "/stop", query={"t": 10})
            container = self._inspect(record)
            if container is not None:
                state = container.get("State", {})
                if state.get("Running") is not False or state.get("Restarting"):
                    return False
                self.api.request("DELETE", "/containers/" + identifier)
            # Successful DELETE, or a later exact-ID 404, is the quiescence proof.
        record["removed"] = True
        atomic_json(self.path, record)
        return True

    def run(self, specification: dict, *, timeout: float = 120) -> None:
        body = copy.deepcopy(specification)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", body.get("Image", "")):
            raise UnsafeInstallation("Docker writer requires an immutable image")
        if self.api.inspect_image(body["Image"]).get("Config", {}).get("Volumes"):
            raise UnsafeInstallation("Docker writer image must not allocate anonymous volumes")
        body.setdefault("HostConfig", {})["RestartPolicy"] = {"Name": "no"}
        if body["HostConfig"].get("AutoRemove"):
            raise UnsafeInstallation("Docker writer must retain its exit status")
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if self.path.exists():
            record = self._read()
            if record.get("specification_hash") != digest:
                raise UnsafeInstallation("Docker writer recovery specification changed")
        else:
            daemon = self.api.request("GET", "/info").get("ID")
            if not daemon:
                raise UnsafeInstallation("Docker writer daemon identity is unavailable")
            token = uuid.uuid4().hex
            record = {
                "protocol_version": 1,
                "token": token,
                "name": "bubo-update-job-" + token,
                "image": body["Image"],
                "specification_hash": digest,
                "daemon_id": daemon,
            }
            atomic_json(self.path, record)
        body.setdefault("Labels", {})["io.bubo.update.job"] = record["token"]
        if record.get("removed") and self._inspect(record) is not None:
            raise UnsafeInstallation("Removed Docker writer unexpectedly exists")
        if not record.get("removed"):
            container = self._inspect(record)
            if container is None:
                if record.get("container_id"):
                    if record.get("exit_code") == 0 and self.stop():
                        return  # Successful exit persisted before a lost DELETE response.
                    raise UnsafeInstallation(
                        "Docker writer disappeared before successful validation"
                    )
                self.api.request("POST", "/containers/create", body, query={"name": record["name"]})
                container = self._inspect(record)
                if container is None:
                    raise UnsafeInstallation("Docker writer creation is unproven")
            record["container_id"] = container["Id"]
            atomic_json(self.path, record)
            try:
                if "exit_code" in record and container.get("State", {}).get(
                    "StartedAt"
                ) != record.get("started_at"):
                    raise UnsafeInstallation("Docker writer restarted after validation")
                if "exit_code" not in record:
                    if container.get("State", {}).get("Status") == "created":
                        self.api.request("POST", "/containers/" + container["Id"] + "/start")
                    deadline = time.monotonic() + timeout
                    while True:
                        container = self._inspect(record)
                        state = (container or {}).get("State", {})
                        started = state.get("StartedAt")
                        if record.get("started_at") and started != record["started_at"]:
                            raise UnsafeInstallation("Docker writer restarted during validation")
                        if (
                            started
                            and not started.startswith("0001-")
                            and "started_at" not in record
                        ):
                            record["started_at"] = started
                            atomic_json(self.path, record)
                        if state.get("Status") == "exited" and state.get("Running") is False:
                            if (
                                not state.get("StartedAt")
                                or state["StartedAt"].startswith("0001-")
                                or type(state.get("ExitCode")) is not int
                            ):
                                raise UnsafeInstallation("Docker writer exit status is unproven")
                            record["exit_code"] = state["ExitCode"]
                            atomic_json(self.path, record)
                            break
                        if container is None or time.monotonic() >= deadline:
                            raise TimeoutError("Docker writer did not complete")
                        time.sleep(0.1)
            finally:
                if not self.stop():
                    raise UnsafeInstallation("Docker writer shutdown is unproven")
        if record.get("exit_code") != 0:
            raise UnsafeInstallation("Docker writer validation failed")

    def reset(self) -> None:
        """Retain one stopped receipt before a freshly restored DB is revalidated."""
        if self.path.exists():
            if not self.stop():
                raise UnsafeInstallation("Docker writer shutdown is unproven")
            self.path.replace(self.path.with_suffix(".previous.json"))
            sync_directory(self.path.parent)
