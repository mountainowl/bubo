import json
import uuid
from contextlib import suppress

import pytest

from bubo.update_identity import atomic_json
from bubo.update_retention import prune, remember


@pytest.fixture
def installation(tmp_path):
    for name in ("attempt.lock", "registry.lock"):
        (tmp_path / name).touch()
    atomic_json(tmp_path / "installation.json", {"states": [str(tmp_path / "state")]})
    return tmp_path


def attempt(installation, phase="committed", **fields):
    identifier = str(uuid.uuid4())
    work = installation / "attempts" / identifier
    work.mkdir(parents=True)
    (work / "database-0.sqlite").write_text("retained data")
    record = {
        "attempt_id": identifier,
        "work": str(work),
        "phase": phase,
        "staged": True,
        "mutated": phase in {"committed", "restored"},
        **fields,
    }
    remember(record)
    return work, record


def test_preserves_current_latest_snapshot_and_two_staged_attempts(installation):
    old, _ = attempt(installation)
    current, record = attempt(installation, "restored")
    newest, _ = attempt(installation)
    staged = [attempt(installation, "deferred")[0] for _ in range(4)]
    atomic_json(installation / "attempt.json", record)
    prune(installation)
    assert not old.exists()
    assert current.exists()
    assert newest.exists()
    assert [item.exists() for item in staged] == [False, False, True, True]
    prune(installation)
    assert (newest / "database-0.sqlite").read_text() == "retained data"


@pytest.mark.parametrize("field", ["environment", "source", "python", "archive", "states"])
def test_never_deletes_registered_active_paths(installation, field):
    old, _ = attempt(installation)
    attempt(installation)
    record = json.loads((installation / "installation.json").read_text())
    record[field] = [str(old / "state")] if field == "states" else str(old / "live")
    atomic_json(installation / "installation.json", record)
    prune(installation)
    assert old.exists()


def test_uncertain_or_unowned_directories_are_preserved(installation):
    uncertain, _ = attempt(installation, "staging")
    failed, _ = attempt(installation, "failed", staged=False)
    malformed, _ = attempt(installation)
    (malformed / "retention.json").write_text("{}")
    unknown, _ = attempt(installation)
    (unknown / "retention.json").unlink()
    attempt(installation)
    prune(installation)
    assert all(work.exists() for work in (uncertain, failed, malformed, unknown))


def test_cleanup_does_not_follow_snapshot_symlinks(installation, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("user data")
    old, _ = attempt(installation)
    (old / "linked-source").symlink_to(outside, target_is_directory=True)
    attempt(installation)
    prune(installation)
    assert not old.exists()
    assert (outside / "keep").read_text() == "user data"


def test_cleanup_is_disabled_during_maintenance(installation):
    old, _ = attempt(installation)
    attempt(installation)
    atomic_json(installation / "maintenance.json", {})
    prune(installation)
    assert old.exists()


def test_cleanup_preserves_mounted_snapshot(installation, monkeypatch):
    from bubo import update_retention

    old, _ = attempt(installation)
    attempt(installation)
    monkeypatch.setattr(update_retention, "_mounted_storage", lambda work: True)
    with pytest.raises(OSError, match="mounted storage"):
        prune(installation)
    assert old.exists()


@pytest.mark.parametrize("interruption", ["before", "during", "after"])
def test_interrupted_removal_resumes_without_inner_receipt(installation, monkeypatch, interruption):
    from bubo import update_retention

    old, _ = attempt(installation)
    newest, _ = attempt(installation)
    remove = update_retention.shutil.rmtree

    def interrupted(work):
        assert (installation / "retention-pending.json").exists()
        if interruption == "during":
            (work / "retention.json").unlink()
        elif interruption == "after":
            remove(work)
        raise KeyboardInterrupt

    interrupted.avoids_symlink_attacks = True
    monkeypatch.setattr(update_retention.shutil, "rmtree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prune(installation)
    monkeypatch.setattr(update_retention.shutil, "rmtree", remove)
    prune(installation)
    assert not old.exists()
    assert newest.exists()
    assert not (installation / "retention-pending.json").exists()


@pytest.mark.parametrize(
    "change", ["replacement", "symlink", "live-path", "last-snapshot", "mount"]
)
def test_resumed_removal_revalidates_protection(installation, monkeypatch, change):
    from bubo import update_retention

    old, _ = attempt(installation)
    newest, _ = attempt(installation)
    remove = update_retention.shutil.rmtree

    def interrupted(work):
        (work / "retention.json").unlink()
        raise KeyboardInterrupt

    interrupted.avoids_symlink_attacks = True
    monkeypatch.setattr(update_retention.shutil, "rmtree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prune(installation)
    monkeypatch.setattr(update_retention.shutil, "rmtree", remove)
    if change in {"replacement", "symlink"}:
        moved = old.with_name(old.name + "-saved")
        old.rename(moved)
        if change == "symlink":
            old.symlink_to(moved, target_is_directory=True)
        else:
            old.mkdir()
    elif change == "live-path":
        atomic_json(installation / "installation.json", {"states": [str(old / "state")]})
    elif change == "last-snapshot":
        remove(newest)
    else:
        monkeypatch.setattr(update_retention, "_mounted_storage", lambda work: True)
    if change in {"replacement", "symlink", "mount"}:
        with pytest.raises(OSError, match=r"redirected|ownership changed|mounted storage"):
            prune(installation)
    else:
        prune(installation)
    assert old.exists()
    assert (installation / "retention-pending.json").exists()


def test_missing_target_removal_is_synced_before_clearing_intent(installation, monkeypatch):
    from bubo import update_retention

    old, _ = attempt(installation)
    stat = old.stat()
    pending = installation / "retention-pending.json"
    atomic_json(
        pending,
        {
            "attempt_id": old.name,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "snapshot": True,
        },
    )
    update_retention.shutil.rmtree(old)
    synced = []

    def sync(path):
        if path == old.parent:
            assert pending.exists()
        else:
            assert path == installation
            assert not pending.exists()
        synced.append(path)

    monkeypatch.setattr(update_retention, "sync_directory", sync)
    prune(installation)
    assert synced == [old.parent, installation]


@pytest.mark.parametrize("phase", ["staging", "failed"])
def test_abandoned_staging_requires_quiescent_writer_receipt(installation, phase):
    work, _ = attempt(installation, phase, staged=False, mutated=False)
    prune(installation)
    assert work.exists()
    writer = work / "stage-writer.json"
    for ownership in ({"state": "starting"}, {"state": "running", "pid": -1, "start": "unknown"}):
        atomic_json(writer, ownership)
        prune(installation)
        assert work.exists()
    atomic_json(writer, {"state": "quiescent"})
    prune(installation)
    assert not work.exists()


def test_staging_receipt_never_authorizes_mutated_attempt_cleanup(installation):
    work, _ = attempt(installation, "failed", staged=False, mutated=True)
    atomic_json(work / "stage-writer.json", {"state": "quiescent"})
    prune(installation)
    assert work.exists()


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX process group recovery")
def test_abandoned_stage_reaps_writer_after_coordinator_death(installation):
    import os
    import signal
    import subprocess
    import sys
    import time

    work, _ = attempt(installation, "staging", staged=False, mutated=False)
    code = """
import sys
from pathlib import Path
from bubo.update_installers import PythonInstaller
from bubo.update_admission import _file_lock
installation, work = map(Path, sys.argv[1:])
installer = PythonInstaller(installation / 'environment', installation)
def stage(release, destination):
    installer._run([sys.executable, '-c', 'import time; time.sleep(60)'])
installer._stage = stage
with _file_lock(installation / 'attempt.lock', exclusive=True):
    installer.stage(None, work)
"""
    process = subprocess.Popen([sys.executable, "-c", code, str(installation), str(work)])
    child = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                record = json.loads((work / "stage-writer.json").read_text())
                if record.get("state") == "running":
                    child = record["pid"]
                    break
            except OSError, ValueError:
                # The record may be mid-write; poll again.
                pass
            time.sleep(0.02)
        assert child is not None
        process.kill()
        process.wait(timeout=5)
        prune(installation)
        assert not work.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if child is not None:
            with suppress(ProcessLookupError):
                os.killpg(child, signal.SIGKILL)
