"""Docker installation adapter for the shared journaled update engine."""

from contextlib import contextmanager
from pathlib import Path

from bubo.update_docker import DockerAPI, DockerError, replacement_preflight, stage_image
from bubo.update_docker_job import DockerJob
from bubo.update_docker_migration import migrate_state
from bubo.update_docker_source import RetainedDockerSource
from bubo.update_identity import atomic_json
from bubo.update_installers import UnsafeInstallation
from bubo.update_readiness import _read
from bubo.update_registration import load_registration
from bubo.updater import Release


class DockerInstaller:
    kind = "docker"

    def __init__(self, api: DockerAPI, state: Path, container_id: str):
        self.api, self.state, self.container_id = api, state.resolve(), container_id
        registration = load_registration(state)
        if registration.get("external") != "docker" or registration.get("view") != "host":
            raise UnsafeInstallation("Docker installation requires an external host controller")
        self.installation = Path(registration["installation"])
        self.root = Path(registration["root"])

    def work(self) -> Path:
        journal = _read(self.installation / "attempt.json")
        path = Path(journal["work"]).resolve()
        if not path.is_relative_to(self.installation / "attempts"):
            raise UnsafeInstallation("Docker attempt directory is outside its installation")
        return path

    def source(self) -> RetainedDockerSource:
        work = self.work()
        path = work / "docker-source.json"
        identifier = _read(path)["container_id"] if path.exists() else self.container_id
        return RetainedDockerSource(
            self.api, identifier, self.root, work, retained_ids=self.retained_ids()
        )

    def retained_ids(self) -> frozenset[str]:
        path = self.installation / "docker-retained.json"
        if not path.exists():
            return frozenset()
        retained = []
        for record in _read(path).get("containers", []):
            try:
                container = self.api.inspect_container(record["id"])
            except DockerError as exc:
                if exc.status == 404:
                    continue
                raise
            if (
                container.get("Id") != record["id"]
                or container.get("Image") != record["image"]
                or container.get("Name") != "/" + record["name"]
                or container.get("State", {}).get("Running") is not False
                or container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") != "no"
            ):
                raise UnsafeInstallation("Retained Docker installation ownership changed")
            retained.append(record["id"])
        return frozenset(retained)

    def cleanup(self) -> None:
        """Keep the latest rollback source; never remove volumes or running containers."""
        from bubo.update_admission import _file_lock
        from bubo.update_identity import atomic_json

        with (
            _file_lock(self.installation / "attempt.lock", exclusive=True),
            _file_lock(self.installation / "registry.lock", exclusive=True),
        ):
            if (self.installation / "maintenance.json").exists():
                return
            path = self.installation / "docker-retained.json"
            if not path.exists():
                return
            retained = _read(path)
            owned = self.retained_ids()
            newest = next(
                (item["id"] for item in reversed(retained["containers"]) if item["id"] in owned),
                None,
            )
            for record in list(retained["containers"]):
                identifier = record["id"]
                if identifier == newest:
                    continue
                if identifier in owned:
                    if self.api.request("GET", "/containers/" + identifier + "/changes"):
                        raise UnsafeInstallation("Retained Docker container has local changes")
                    self.api.request(
                        "DELETE",
                        "/containers/" + identifier,
                        query={
                            "force": "false",
                            "v": "false",
                        },
                    )
                    try:
                        self.api.inspect_container(identifier)
                    except DockerError as exc:
                        if exc.status != 404:
                            raise
                    else:
                        raise UnsafeInstallation("Retained Docker container removal is unproven")
                retained["containers"].remove(record)
                atomic_json(path, retained)

    def stage(self, release: Release, destination: Path) -> None:
        from bubo.update_docker_runtime import resident_kind

        descriptor = replacement_preflight(
            self.api, self.container_id, self.root, retained_ids=self.retained_ids()
        )
        resident_kind(descriptor["create"], self.root)
        image = self.api.inspect_image(descriptor["image_id"])
        stage_image(self.api, release, destination, architecture=image["Architecture"])

    @contextmanager
    def maintenance(self):
        if _read(self.installation / "attempt.json").get("states") != [str(self.state)]:
            raise UnsafeInstallation("Docker updates require one mapped persistent state")
        yield

    def snapshot(self, destination: Path) -> None:
        if destination.resolve() != self.work():
            raise UnsafeInstallation("Docker snapshot attempt changed")
        descriptor = self.source().create_spec()  # Proves retained, stopped source.
        atomic_json(
            destination / "snapshot.json",
            {
                "kind": self.kind,
                "image_id": descriptor["Image"],
            },
        )

    def activate(self, staged: Path) -> None:
        if staged.resolve() != self.work():
            raise UnsafeInstallation("Docker activation attempt changed")
        package = _read(staged / "docker-image.json")
        journal = _read(self.installation / "attempt.json")
        if package.get("version") != journal["target"]:
            raise UnsafeInstallation("Staged Docker release changed")
        image = self.api.inspect_image(package["image_id"])
        if image.get("Id") != package["image_id"] or package.get("digest") not in image.get(
            "RepoDigests", []
        ):
            raise UnsafeInstallation("Staged Docker image identity changed")
        atomic_json(staged / "docker-active.json", package)

    def selected_image(self) -> str:
        return _read(self.work() / "docker-active.json")["image_id"]

    def migrate_and_check(self, states: list[Path], token: str) -> None:
        journal = _read(self.installation / "attempt.json")
        if states != [self.state] or journal.get("token") != token:
            raise UnsafeInstallation("Docker migration state or token changed")
        descriptor = self.source().create_spec()
        migrate_state(
            self.api,
            self.state,
            self.work(),
            self.selected_image(),
            user=descriptor.get("User", ""),
        )

    def writers_quiescent(self) -> bool:
        return all(
            DockerJob(self.api, self.work() / name).stop()
            for name in ("docker-migration.json", "docker-migration-restored.json")
        )

    def recovery_ready(self, snapshot: Path) -> bool:
        try:
            return (
                snapshot.resolve() == self.work()
                and _read(snapshot / "snapshot.json")["kind"] == self.kind
                and self.source().create_spec()["Image"]
                == _read(snapshot / "snapshot.json")["image_id"]
            )
        except KeyError, OSError, ValueError, UnsafeInstallation:
            return False

    def restore(self, snapshot: Path) -> None:
        if not self.writers_quiescent() or not self.recovery_ready(snapshot):
            raise UnsafeInstallation("Docker source recovery is unavailable")
        DockerJob(self.api, snapshot / "docker-migration-restored.json").reset()
        original = _read(snapshot / "snapshot.json")
        atomic_json(snapshot / "docker-active.json", original)

    def verify_restored(self) -> bool:
        return (
            self.recovery_ready(self.work())
            and self.selected_image() == _read(self.work() / "snapshot.json")["image_id"]
        )

    def generation_version(self, version: str) -> str:
        return version
