"""Execute the shared DB migration contract in the selected immutable image."""

from pathlib import Path, PurePosixPath

from bubo.update_docker import DockerAPI
from bubo.update_docker_job import DockerJob
from bubo.update_installers import MIGRATION_SCRIPT, UnsafeInstallation
from bubo.update_readiness import _read
from bubo.update_registration import load_registration


def migrate_state(api: DockerAPI, state: Path, work: Path, image: str, *, user: str) -> None:
    """Caller has fenced/excluded all writers and retained matched DB backups."""
    registration = load_registration(state)
    if registration.get("external") != "docker" or registration.get("view") != "host":
        raise UnsafeInstallation("Docker migration requires the external host controller")
    installation = Path(registration["installation"])
    journal = _read(installation / "attempt.json")
    fence = _read(installation / "maintenance.json")
    if (
        journal.get("phase") not in {"migration", "recovery"}
        or str(state.resolve()) not in journal.get("states", [])
        or not journal.get("token")
        or not journal.get("attempt_id")
        or any(journal.get(key) != fence.get(key) for key in ("attempt_id", "token"))
        or Path(journal.get("work", "")).resolve() != work.resolve()
        or not work.resolve().is_relative_to(installation.resolve() / "attempts")
    ):
        raise UnsafeInstallation("Docker migration attempt is not authorized")
    container = registration["mapping"]["container"]
    base = str(PurePosixPath(container["state"]).parent)
    environment = {
        "BUBO_ROOT": container["root"],
        "BUBO_BASE_DIR": base,
        "BUBO_UPDATE_EXPECTED_DB": str(PurePosixPath(container["state"]) / "reviewer.sqlite"),
        "BUBO_MAINTENANCE_TOKEN": journal["token"],
    }
    # Only the mapped Bubo root is mounted. SCM/LLM credentials, workspaces and
    # the application's environment are not needed by schema migrations.
    record = (
        "docker-migration-restored.json"
        if journal["phase"] == "recovery"
        else "docker-migration.json"
    )
    DockerJob(api, work / record).run(
        {
            "Image": image,
            "User": user,
            "Entrypoint": ["python", "-c"],
            "Cmd": [MIGRATION_SCRIPT],
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "NetworkDisabled": True,
            "HostConfig": {
                "NetworkMode": "none",
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "PidsLimit": 64,
                "Memory": 536870912,
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=64m"},
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": registration["root"],
                        "Target": container["root"],
                        "ReadOnly": False,
                    }
                ],
            },
        }
    )
