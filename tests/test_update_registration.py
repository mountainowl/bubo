"""Host/container mapping contracts; these tests do not emulate a Docker daemon."""

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from bubo import update_admission, update_helper, update_readiness, update_supervisor
from bubo.update_admission import MaintenanceInProgress, activity, exclusive
from bubo.update_docker_migration import migrate_state
from bubo.update_identity import atomic_json
from bubo.update_installers import UnsafeInstallation
from bubo.update_readiness import ResidentReadiness, ResidentStartup
from bubo.update_registration import journal_path, load_registration


@pytest.fixture
def mapped(tmp_path, monkeypatch):
    mapping = {}
    for side in ("host", "container"):
        root = tmp_path / side
        state = root / "var/state"
        installation = root / ".updates/installation"
        state.mkdir(parents=True)
        installation.mkdir(parents=True)
        (root / "config").mkdir()
        (root / "config/env.toml").write_text("auto_update = false\n")
        mapping[side] = {"root": str(root), "state": str(state), "installation": str(installation)}
    record = {"protocol_version": 1, "external": "docker", "ready": True, "mapping": mapping}
    for side in mapping.values():
        atomic_json(Path(side["state"]) / "update-registration.json", record)
    # Share the same lock inodes, as a real bind-mounted root would.
    for key, names in (
        ("installation", ("registry.lock", "activity.lock")),
        ("state", ("update-activity.lock",)),
    ):
        for name in names:
            host = Path(mapping["host"][key]) / name
            host.touch()
            os.link(host, Path(mapping["container"][key]) / name)
    monkeypatch.setattr(update_admission, "_PROCESS_VERSION", "0.27.0")
    monkeypatch.setattr(update_readiness, "installed_version", lambda: "0.27.0")
    return mapping


def test_same_record_resolves_from_both_registered_views(mapped):
    for view, expected in mapped.items():
        state = Path(expected["state"])
        record = load_registration(state)
        assert record["view"] == view
        assert record["root"] == expected["root"]
        assert record["installation"] == expected["installation"]
        host_file = Path(mapped["host"]["installation"]) / "attempts/backup.sqlite"
        assert (
            journal_path(state, str(host_file))
            == Path(expected["installation"]) / "attempts/backup.sqlite"
        )
        assert update_helper._raw_config(state) == {"auto_update": False}


@pytest.mark.parametrize(
    "mutation", ["outside", "relative", "parent", "different_suffix", "wrong_state", "unknown"]
)
def test_invalid_mapping_cannot_redirect_runtime(mapped, mutation):
    state = Path(mapped["container"]["state"])
    path = state / "update-registration.json"
    record = json.loads(path.read_text())
    target = record["mapping"]["container"]
    if mutation == "outside":
        target["installation"] = "/elsewhere"
    elif mutation == "relative":
        target["root"] = "relative"
    elif mutation == "parent":
        target["state"] = target["root"] + "/var/../state"
    elif mutation == "different_suffix":
        target["installation"] = target["root"] + "/different"
    elif mutation == "wrong_state":
        for side in record["mapping"].values():
            side["state"] = side["root"] + "/different/state"
    else:
        record["external"] = "unknown"
    atomic_json(path, record)
    with pytest.raises(ValueError, match=r"Update|State|Unknown"):
        load_registration(state)


def test_identical_views_do_not_grant_host_command_dispatch(mapped):
    host = mapped["host"]
    state = Path(host["state"])
    atomic_json(
        state / "update-registration.json",
        {
            "protocol_version": 1,
            "external": "docker",
            "mapping": {"host": host, "container": host},
        },
    )
    assert load_registration(state)["view"] == "shared"
    assert journal_path(state, host["installation"]) == Path(host["installation"])


def test_host_exclusion_blocks_container_admission(mapped):
    host = mapped["host"]
    with exclusive(Path(host["installation"]), [Path(host["state"])]):
        with pytest.raises(MaintenanceInProgress), activity(Path(mapped["container"]["state"])):
            pytest.fail("Container work bypassed host maintenance")


def test_installation_symlink_escape_is_rejected(mapped, tmp_path):
    container = mapped["container"]
    path = Path(container["installation"])
    path.rename(tmp_path / "outside-installation")
    path.symlink_to(tmp_path / "outside-installation", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink escapes"):
        load_registration(Path(container["state"]))


def test_journal_descendant_symlink_escape_is_rejected(mapped, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    container = mapped["container"]
    (Path(container["installation"]) / "attempts").symlink_to(outside, target_is_directory=True)
    host_path = Path(mapped["host"]["installation"]) / "attempts/backup.sqlite"
    with pytest.raises(ValueError, match="symlink escapes"):
        journal_path(Path(container["state"]), str(host_path))


@pytest.mark.parametrize("outcome", ["committed", "restored"])
def test_container_startup_uses_host_journal_and_local_paths(mapped, monkeypatch, outcome):
    host, container = mapped["host"], mapped["container"]
    attempt = {
        "attempt_id": str(uuid.uuid4()),
        "token": uuid.uuid4().hex,
        "phase": "readiness" if outcome == "committed" else "recovery",
        "states": [host["state"]],
        "source": "0.27.0" if outcome == "restored" else "0.26.0",
        "target": "0.27.0",
    }
    for side in mapped.values():
        installation = Path(side["installation"])
        atomic_json(installation / "attempt.json", attempt)
        atomic_json(installation / "maintenance.json", attempt)
    receipt = ResidentReadiness(Path(host["state"]), "mcp-http", outcome=outcome)
    # Share resident artifacts; only journal path translation is under test.
    (Path(container["installation"]) / "residents").symlink_to(
        Path(host["installation"]) / "residents", target_is_directory=True
    )
    monkeypatch.setenv(update_readiness.REQUEST_ENV, receipt.request_id)
    gate = ResidentStartup.from_environment("mcp-http", Path(container["state"]))
    gate.signal_ready()
    assert not gate.committed()
    attempt["phase"] = outcome
    for side in mapped.values():
        installation = Path(side["installation"])
        atomic_json(installation / "attempt.json", attempt)
        atomic_json(
            installation / "generation.json",
            {
                "version": "0.27.0",
                "activation": {
                    "attempt_id": attempt["attempt_id"],
                    "token": attempt["token"],
                    "outcome": outcome,
                    "states": attempt["states"],
                },
            },
        )
        (installation / "maintenance.json").unlink()
    assert gate.committed()
    assert json.loads(receipt.ready_path.read_text())["state"] == host["state"]
    # Docker reuses its create-time environment on ordinary later restarts.
    original_receipt = receipt.ready_path.read_bytes()
    monkeypatch.setenv(update_readiness.REQUEST_ENV, receipt.request_id)
    assert ResidentStartup.from_environment("mcp-http", Path(container["state"])) is None
    assert receipt.ready_path.read_bytes() == original_receipt
    # Later nonmutating attempts must not invalidate the active generation.
    for phase in ("staging", "deferred", "failed"):
        atomic_json(
            Path(container["installation"]) / "attempt.json",
            {
                "attempt_id": str(uuid.uuid4()),
                "token": uuid.uuid4().hex,
                "phase": phase,
                "target": "0.28.0",
                "source": "0.27.0",
                "states": attempt["states"],
            },
        )
        monkeypatch.setenv(update_readiness.REQUEST_ENV, receipt.request_id)
        assert ResidentStartup.from_environment("mcp-http", Path(container["state"])) is None
        assert gate.committed()
    # A same-version foreign activation is as invalid as a stale code version.
    generation_path = Path(container["installation"]) / "generation.json"
    generation = json.loads(generation_path.read_text())
    for invalid in (
        {**generation, "version": "0.28.0"},
        {**generation, "activation": {**generation["activation"], "token": "foreign"}},
        {**generation, "activation": {**generation["activation"], "states": []}},
        {"version": "0.27.0"},
    ):
        atomic_json(generation_path, invalid)
        monkeypatch.setenv(update_readiness.REQUEST_ENV, receipt.request_id)
        with pytest.raises(update_readiness.StartupRejected, match="generation"):
            ResidentStartup.from_environment("mcp-http", Path(container["state"]))


def test_external_registration_never_bootstraps_native_helper(mapped, monkeypatch):
    monkeypatch.setattr(
        update_helper, "bootstrap", lambda *args, **kwargs: pytest.fail("native bootstrap")
    )
    for side in mapped.values():
        update_helper.ensure_registered(Path(side["root"]), state=Path(side["state"]))
        assert not (Path(side["state"]) / "update-registration-conflict.json").exists()


def test_container_delegate_and_cli_do_not_execute_host_programs(mapped, monkeypatch, capsys):
    from bubo import paths

    container = mapped["container"]
    monkeypatch.setattr(paths, "ROOT", Path(container["root"]))
    monkeypatch.setattr(paths, "DB", Path(container["state"]) / "reviewer.sqlite")
    monkeypatch.delenv(update_supervisor.CHILD_ENV)
    monkeypatch.delenv("BUBO_BASE_DIR", raising=False)
    monkeypatch.setattr(os, "execve", lambda *args: pytest.fail("host exec in container"))
    update_supervisor.delegate("service")
    assert update_helper.cli_command(Path(container["root"]), "update") == 75
    assert "external host controller" in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["migration", "recovery"])
def test_docker_migration_runs_shared_script_with_only_scoped_inputs(mapped, monkeypatch, phase):
    host, container = mapped["host"], mapped["container"]
    state = Path(host["state"])
    work = Path(host["installation"]) / "attempts" / str(uuid.uuid4())
    work.mkdir(parents=True)
    attempt = {
        "attempt_id": work.name,
        "token": uuid.uuid4().hex,
        "phase": phase,
        "states": [str(state)],
        "work": str(work),
    }
    for side in mapped.values():
        atomic_json(Path(side["installation"]) / "attempt.json", attempt)
        atomic_json(Path(side["installation"]) / "maintenance.json", attempt)
    database = Path(container["state"]) / "reviewer.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE user_data (value TEXT)")
        connection.execute("INSERT INTO user_data VALUES ('preserve me')")
    seen = []

    def execute_script_locally(job, body):
        # Executes the real migration code; this is not a real Docker bind mount.
        seen.append(body)
        expected_record = (
            "docker-migration-restored.json" if phase == "recovery" else "docker-migration.json"
        )
        assert job.path.name == expected_record
        environment = dict(item.split("=", 1) for item in body["Env"])
        assert set(environment) == {
            "BUBO_ROOT",
            "BUBO_BASE_DIR",
            "BUBO_UPDATE_EXPECTED_DB",
            "BUBO_MAINTENANCE_TOKEN",
        }
        environment.update(
            PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
            HOME=container["root"],
            PATH=os.defpath,
        )
        result = subprocess.run(
            [sys.executable, "-c", body["Cmd"][0]],
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr

    monkeypatch.setattr("bubo.update_docker_migration.DockerJob.run", execute_script_locally)
    migrate_state(None, state, work, "sha256:" + "a" * 64, user="10001")
    assert len(seen) == 1
    assert seen[0]["HostConfig"]["Mounts"] == [
        {
            "Type": "bind",
            "Source": host["root"],
            "Target": container["root"],
            "ReadOnly": False,
        }
    ]
    assert seen[0]["HostConfig"]["NetworkMode"] == "none"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM user_data").fetchall() == [("preserve me",)]
        assert (
            connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[
                0
            ]
            > 1
        )


@pytest.mark.parametrize("change", ["phase", "token", "state", "work"])
def test_docker_migration_rejects_unfenced_or_mismatched_attempt(mapped, monkeypatch, change):
    host = mapped["host"]
    state = Path(host["state"])
    work = Path(host["installation"]) / "attempts" / str(uuid.uuid4())
    work.mkdir(parents=True)
    journal = {
        "attempt_id": work.name,
        "token": uuid.uuid4().hex,
        "phase": "migration",
        "states": [str(state)],
        "work": str(work),
    }
    atomic_json(Path(host["installation"]) / "maintenance.json", journal)
    if change == "phase":
        journal["phase"] = "staging"
    elif change == "token":
        journal["token"] = "different-token"
    elif change == "state":
        journal["states"] = []
    else:
        journal["work"] = host["root"]
    atomic_json(Path(host["installation"]) / "attempt.json", journal)
    monkeypatch.setattr(
        "bubo.update_docker_migration.DockerJob.run",
        lambda *args: pytest.fail("Unauthorized Docker writer started"),
    )
    with pytest.raises(UnsafeInstallation, match="not authorized"):
        migrate_state(None, state, work, "sha256:" + "a" * 64, user="10001")
