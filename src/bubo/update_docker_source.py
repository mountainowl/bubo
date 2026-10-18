"""Retain the original Docker container as the private launch/recovery authority.

Call only under the installation attempt lock, maintenance fence and exclusive
admission. No DB backup or restore is permitted until retain() proves shutdown.
The daemon keeps credentials; the journal stores identities and salted hashes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path

from bubo.update_docker import DockerAPI, replacement_preflight
from bubo.update_identity import atomic_json
from bubo.update_installers import UnsafeInstallation


class RetainedDockerSource:
    def __init__(
        self,
        api: DockerAPI,
        identifier: str,
        root: Path,
        work: Path,
        *,
        retained_ids: frozenset[str] = frozenset(),
    ):
        self.api, self.identifier, self.root = api, identifier, root
        self.path = work / "docker-source.json"
        self.retained_ids = retained_ids

    def _hash(self, value: dict, token: str) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256((token + payload).encode()).hexdigest()

    def _daemon(self, token: str) -> str:
        info = self.api.request("GET", "/info")
        if not isinstance(info, dict) or not info.get("ID"):
            raise UnsafeInstallation("Docker daemon identity is unavailable")
        # Exclude counters and live resource usage; retain ownership, platform,
        # security/runtime defaults and daemon release across interruption.
        fields = (
            "ID",
            "SecurityOptions",
            "Rootless",
            "DockerRootDir",
            "Driver",
            "DefaultRuntime",
            "Runtimes",
            "CgroupDriver",
            "CgroupVersion",
            "OSType",
            "Architecture",
            "ServerVersion",
            "InitBinary",
            "LiveRestoreEnabled",
        )
        return self._hash({key: info.get(key) for key in fields}, token)

    def _read(self) -> dict:
        value = json.loads(self.path.read_text())
        if value.get("protocol_version") != 1 or value.get("container_id") != self.identifier:
            raise UnsafeInstallation("Retained Docker source identity changed")
        return value

    def _inspect(self, record: dict) -> dict:
        if self._daemon(record["token"]) != record["daemon"]:
            raise UnsafeInstallation("Docker daemon identity or defaults changed")
        descriptor = replacement_preflight(
            self.api, self.identifier, self.root, retained_ids=self.retained_ids
        )
        if (
            descriptor["container_id"] != record["container_id"]
            or descriptor["image_id"] != record["image_id"]
            or descriptor["name"] not in {record["name"], record["retained_name"]}
        ):
            raise UnsafeInstallation("Retained Docker source identity changed")
        spec = copy.deepcopy(descriptor["create"])
        endpoints = spec["NetworkingConfig"]["EndpointsConfig"]
        for network, mac in record.get("network_macs", {}).items():
            # Docker clears assigned MACs on stop. Retain only these nonsecret
            # addresses; all credentials and other configuration stay in Docker.
            endpoint = endpoints.get(network)
            if endpoint is not None and not endpoint.get("MacAddress"):
                endpoint["MacAddress"] = mac
        policy = spec["HostConfig"].get("RestartPolicy", {})
        allowed_policies = (
            record["restart_policy"],
            {"Name": "no"},
            {"Name": "no", "MaximumRetryCount": 0},
        )
        if policy not in allowed_policies:
            raise UnsafeInstallation("Retained Docker source restart policy changed")
        spec["HostConfig"]["RestartPolicy"] = record["restart_policy"]
        if self._hash(spec, record["token"]) != record["launch_hash"]:
            raise UnsafeInstallation("Retained Docker launch configuration changed")
        descriptor["create"]["NetworkingConfig"] = spec["NetworkingConfig"]
        return descriptor

    def retain(self) -> dict:
        """Disable restart, stop and rename with durable intent before any mutation.

        Reentry inspects exact IDs after each ambiguous response. It never deletes
        or starts the original container and never copies its environment to disk.
        """
        if self.path.exists():
            record = self._read()
        else:
            descriptor = replacement_preflight(
                self.api, self.identifier, self.root, retained_ids=self.retained_ids
            )
            if descriptor["container_id"] != self.identifier:
                raise UnsafeInstallation("A complete Docker source ID is required")
            token = uuid.uuid4().hex
            endpoints = descriptor["create"]["NetworkingConfig"]["EndpointsConfig"]
            record = {
                "protocol_version": 1,
                "container_id": self.identifier,
                "token": token,
                "image_id": descriptor["image_id"],
                "name": descriptor["name"],
                "retained_name": "bubo-retained-" + token,
                "restart_policy": descriptor["create"]["HostConfig"].get("RestartPolicy", {}),
                "was_running": descriptor["running"],
                "network_macs": {
                    name: endpoint["MacAddress"]
                    for name, endpoint in endpoints.items()
                    if endpoint.get("MacAddress")
                },
                "launch_hash": self._hash(descriptor["create"], token),
                "daemon": self._daemon(token),
                "phase": "intent",
            }
            atomic_json(self.path, record)
        descriptor = self._inspect(record)
        stop_timeout = descriptor["create"].get("StopTimeout", 10)
        if type(stop_timeout) is not int or not 0 <= stop_timeout <= 300:
            raise UnsafeInstallation("Docker source needs a bounded shutdown timeout")
        policy = descriptor["create"]["HostConfig"].get("RestartPolicy", {})
        if policy.get("Name") != "no":
            self.api.request(
                "POST",
                "/containers/" + self.identifier + "/update",
                {
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                },
            )
        descriptor = self._inspect(record)
        if descriptor["create"]["HostConfig"].get("RestartPolicy", {}).get("Name") != "no":
            raise UnsafeInstallation("Docker source restart suppression is unproven")
        if descriptor["running"]:
            self.api.request(
                "POST", "/containers/" + self.identifier + "/stop", query={"t": stop_timeout}
            )
        descriptor = self._inspect(record)
        if descriptor["running"]:
            raise UnsafeInstallation("Docker source shutdown is unproven")
        if descriptor["name"] != record["retained_name"]:
            self.api.request(
                "POST",
                "/containers/" + self.identifier + "/rename",
                query={
                    "name": record["retained_name"],
                },
            )
        descriptor = self._inspect(record)
        if descriptor["running"] or descriptor["name"] != record["retained_name"]:
            raise UnsafeInstallation("Docker source retention is unproven")
        record["phase"] = "retained"
        atomic_json(self.path, record)
        return record

    def create_spec(self) -> dict:
        """Read credentials from the stopped source only when creating a candidate.

        Keep restart disabled until the engine's durable terminal decision and
        finalization. The caller selects the target/restored image and startup gate.
        Recovery must stop and remove its owned candidate before calling this;
        shared containers are deliberately not exempted from preflight here.
        """
        record = self._read()
        descriptor = self._inspect(record)
        if (
            record.get("phase") != "retained"
            or descriptor["running"]
            or descriptor["name"] != record["retained_name"]
            or descriptor["create"]["HostConfig"].get("RestartPolicy", {}).get("Name") != "no"
        ):
            raise UnsafeInstallation("Docker source retention is unproven")
        spec = descriptor["create"]
        # Restoring through the old mutable tag could silently select new code.
        # A target candidate explicitly replaces this with its staged image ID.
        spec["Image"] = record["image_id"]
        return spec
