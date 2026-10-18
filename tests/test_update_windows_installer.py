import json
import os
import sys

import pytest

from bubo import update_installers, update_windows_files, update_windows_process
from bubo.update_identity import atomic_json


@pytest.fixture
def installer(tmp_path, monkeypatch):
    installer = update_installers.PythonInstaller(
        tmp_path / "env", tmp_path / "controller", executables={}
    )
    monkeypatch.setattr(sys, "platform", "win32")
    # Simulated Jobs run on POSIX too; native file publication has its own tests.
    monkeypatch.setattr(update_windows_files, "replace_file", os.replace)
    monkeypatch.setattr(update_installers, "_start_identity", lambda pid: f"identity:{pid}")
    return installer


def test_windows_writer_records_owned_job_before_child_and_recovers(installer, monkeypatch):
    recovered = []

    def capture(args, *, name, env, timeout, on_started, on_job_created):
        record = json.loads(installer.writer.read_text())
        assert record["job_created"] is False
        assert "memory-secret" not in installer.writer.read_text()
        on_job_created()
        assert json.loads(installer.writer.read_text())["job_created"] is True
        on_started(123)
        assert json.loads(installer.writer.read_text())["pid"] == 123
        assert env["TOKEN"] == "memory-secret"
        return b"ok"

    monkeypatch.setattr(update_windows_process, "run_capture", capture)
    monkeypatch.setattr(update_windows_process, "recover_job", lambda name: recovered.append(name))
    assert installer._run(["/python.exe"], mutation=True, env={"TOKEN": "memory-secret"}) == "ok"
    assert len(recovered) == 1
    assert recovered[0].startswith("Global\\BuboUpdate-")
    assert json.loads(installer.writer.read_text()) == {"state": "quiescent"}


def test_job_creation_collision_does_not_terminate_unowned_job(installer, monkeypatch):
    def refused(*args, **kwargs):
        raise OSError("existing job")

    def must_not_recover(*args):
        raise AssertionError("Never terminate a job whose creation was not confirmed")

    monkeypatch.setattr(update_windows_process, "run_capture", refused)
    monkeypatch.setattr(update_windows_process, "recover_job", must_not_recover)
    with pytest.raises(OSError, match="existing job"):
        installer._run(["/python.exe"], mutation=True)
    assert json.loads(installer.writer.read_text()) == {"state": "quiescent"}


@pytest.mark.parametrize(("owner_live", "published"), [(True, True), (False, True), (False, False)])
def test_windows_recovery_requires_dead_owner_before_touching_job(
    installer,
    monkeypatch,
    owner_live,
    published,
):
    owner_pid = os.getpid() + 100
    atomic_json(
        installer.writer,
        {
            "state": "starting",
            "job_created": published,
            "owner": {"pid": owner_pid, "start": f"identity:{owner_pid}"},
            "job": update_windows_process.new_job_name(),
        },
    )
    monkeypatch.setattr(
        update_installers, "_start_identity", lambda pid: f"identity:{pid}" if owner_live else None
    )
    recovered = []
    monkeypatch.setattr(update_windows_process, "recover_job", lambda name: recovered.append(name))
    assert installer.writers_quiescent() is not owner_live
    assert len(recovered) == int(not owner_live and published)


def test_unproven_job_shutdown_keeps_writer_unresolved(installer, monkeypatch):
    def capture(*args, on_job_created, **kwargs):
        on_job_created()
        return b"ok"

    def unavailable(name):
        raise OSError("job membership unavailable")

    monkeypatch.setattr(update_windows_process, "run_capture", capture)
    monkeypatch.setattr(update_windows_process, "recover_job", unavailable)
    with pytest.raises(OSError, match="membership unavailable"):
        installer._run(["/python.exe"], mutation=True)
    assert json.loads(installer.writer.read_text())["state"] == "starting"


@pytest.mark.parametrize("location", ["snapshot", "environment", "recovery", "failed"])
def test_restore_rejects_reparse_tree_before_mutation(installer, tmp_path, monkeypatch, location):
    import stat
    from pathlib import Path
    from types import SimpleNamespace

    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "snapshot.json").write_text(
        json.dumps({"environment": str(installer.environment)})
    )
    saved = destination / "environment"
    saved.mkdir()
    (saved / "original").write_text("saved data")
    installer.environment.mkdir()
    (installer.environment / "current").write_text("live data")
    roots = {
        "snapshot": saved,
        "environment": installer.environment,
        "recovery": installer.environment.with_name("env.bubo-recovery"),
        "failed": installer.environment.with_name("env.bubo-failed"),
    }
    root = roots[location]
    root.mkdir(exist_ok=True)
    redirected = root / "junction"
    redirected.mkdir()
    original = Path.lstat

    def metadata(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == redirected:
            return SimpleNamespace(
                st_mode=value.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        return value

    monkeypatch.setattr(Path, "lstat", metadata)
    with pytest.raises(update_installers.UnsafeInstallation, match="reparse point"):
        installer.restore(destination)
    assert (installer.environment / "current").read_text() == "live data"
    assert (saved / "original").read_text() == "saved data"
    assert redirected.is_dir()


def test_windows_tree_checks_redirected_ancestors(tmp_path, monkeypatch):
    parent = tmp_path / "redirected"
    target = tmp_path / "target"
    target.mkdir()
    parent.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(update_installers.UnsafeInstallation, match="reparse point"):
        update_installers._check_windows_tree(parent / "missing")
    assert list(target.iterdir()) == []


def test_windows_tree_accepts_ordinary_and_missing_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "file").write_text("data")
    update_installers._check_windows_tree(tmp_path / "plain")
    update_installers._check_windows_tree(tmp_path / "missing")


@pytest.mark.parametrize("operation", ["preflight", "snapshot", "activate", "restore"])
def test_source_operations_reject_redirected_source_before_inspection(
    tmp_path, monkeypatch, operation
):
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "valuable").write_text("untouched")
    (source / "link").symlink_to(external, target_is_directory=True)
    adapter = update_installers.SourceInstaller(
        tmp_path / "environment", tmp_path / "controller", source, executables={}
    )
    monkeypatch.setattr(sys, "platform", "win32")
    args = [] if operation == "preflight" else [tmp_path / "snapshot"]
    with pytest.raises(update_installers.UnsafeInstallation, match="reparse point"):
        getattr(adapter, operation)(*args)
    assert (external / "valuable").read_text() == "untouched"
