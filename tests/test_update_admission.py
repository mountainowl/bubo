from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

from bubo.update_admission import (
    MaintenanceInProgress,
    UpdateBusy,
    activity,
    environment_lock,
    exclusive,
    handoff_fds,
    resident_restart,
    restart_required,
)


@pytest.fixture
def registered(tmp_path):
    installation = tmp_path / "installation"
    state = tmp_path / "state"
    installation.mkdir()
    state.mkdir()
    for name in ("registry.lock", "activity.lock"):
        (installation / name).touch()
    (state / "update-activity.lock").touch()
    (state / "update-registration.json").write_text(json.dumps({"installation": str(installation)}))
    return installation, state


def test_unregistered_activity_never_creates_files(tmp_path):
    state = tmp_path / "missing"
    with activity(state):
        assert not state.exists()
    assert not state.exists()


@pytest.mark.parametrize("without_fcntl", [False, True])
def test_unregistered_readonly_existing_state_creates_no_files(
    tmp_path, monkeypatch, without_fcntl
):
    from bubo import update_admission as admission

    state = tmp_path / "state"
    state.mkdir()
    if without_fcntl:
        monkeypatch.setattr(admission, "fcntl", None)
    with activity(state, writable=False):
        assert list(state.iterdir()) == []
    assert list(state.iterdir()) == []


def test_registration_published_before_provisional_lease_rechecks_generation(
    registered, monkeypatch
):
    from contextlib import contextmanager

    from bubo import update_admission as admission

    installation, state = registered
    registration = state / "update-registration.json"
    record = registration.read_text()
    registration.unlink()
    original = admission._file_lock
    published = False

    @contextmanager
    def racing_lock(path, **kwargs):
        nonlocal published
        if path == state and not published:
            published = True
            registration.write_text(record)
            (installation / "generation.json").write_text('{"version":"different"}')
        with original(path, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(admission, "_file_lock", racing_lock)
    with pytest.raises(MaintenanceInProgress), activity(state):
        pytest.fail("Old code entered after a newly registered update")
    assert published


def test_unregistered_activity_blocks_first_registration_without_writing(tmp_path, monkeypatch):
    environment = tmp_path / "environment"
    environment.mkdir()
    configuration = environment / "pyvenv.cfg"
    configuration.write_text("home = fixture\n")
    monkeypatch.setattr(sys, "prefix", str(environment))
    state = tmp_path / "unregistered"
    with activity(state):
        with pytest.raises(UpdateBusy), environment_lock(environment, exclusive=True):
            pass
    with environment_lock(environment, exclusive=True):
        pass
    assert configuration.read_text() == "home = fixture\n"
    assert not state.exists()


def test_unknown_root_cannot_escape_existing_installation_maintenance(tmp_path, monkeypatch):
    installation = tmp_path / "installation"
    installation.mkdir()
    (installation / "installation.json").write_text("{}")
    (installation / "activity.lock").touch()
    (installation / "registry.lock").touch()
    monkeypatch.setattr(
        "bubo.update_helper.installation_for_environment", lambda *a, **kw: installation
    )
    state = tmp_path / "unregistered"
    with activity(state), pytest.raises(UpdateBusy), exclusive(installation, []):
        pass
    (installation / "maintenance.json").write_text("{}")
    with pytest.raises(MaintenanceInProgress, match="maintenance"), activity(state):
        pass
    assert not state.exists()


def test_resident_restart_waits_for_commit_and_is_quiet(registered, monkeypatch, capsys):
    installation, state = registered
    monkeypatch.setattr("bubo.update_admission._PROCESS_VERSION", "0.27.0")
    (installation / "generation.json").write_text('{"version":"0.28.0"}')
    fence = installation / "maintenance.json"
    fence.write_text("{}")
    restarted = threading.Event()
    assert not restart_required(state)
    with resident_restart(restarted.set, state=state, interval=0.01):
        assert not restarted.wait(0.03)
        fence.unlink()
        assert restarted.wait(1)
    assert capsys.readouterr().out == ""


def test_resident_restart_does_not_act_during_exclusive_maintenance(registered, monkeypatch):
    installation, state = registered
    monkeypatch.setattr("bubo.update_admission._PROCESS_VERSION", "old")
    (installation / "generation.json").write_text('{"version":"new"}')
    with exclusive(installation, [state]):
        assert not restart_required(state)
    assert restart_required(state)


def test_resident_exec_preserves_original_invocation(monkeypatch):
    from bubo.update_admission import restart_process

    calls = []
    monkeypatch.setattr(sys, "orig_argv", ["python", "-m", "bubo.cli", "service", "run"])
    monkeypatch.setattr("os.execv", lambda executable, argv: calls.append((executable, argv)))
    restart_process()
    assert calls == [(sys.executable, [sys.executable, "-m", "bubo.cli", "service", "run"])]


def test_busy_work_is_never_fenced_or_delayed(registered):
    installation, state = registered
    with activity(state):
        for _ in range(10):
            with pytest.raises(UpdateBusy), exclusive(installation, [state]):
                pytest.fail("Updated during work")
            assert not (installation / "maintenance.json").exists()
        with activity(state):  # nested finite operations are reentrant
            pass
    with exclusive(installation, [state]):
        pass


def test_exclusive_owner_excludes_new_work(registered):
    installation, state = registered
    with exclusive(installation, [state]):
        with pytest.raises(MaintenanceInProgress), activity(state):
            pytest.fail("Admitted during maintenance")
    with activity(state):
        pass


def test_crash_fence_survives_os_lock_release(registered):
    installation, state = registered
    with exclusive(installation, [state]):
        (installation / "maintenance.json").write_text('{"phase":"installing"}')
    with pytest.raises(MaintenanceInProgress), activity(state):
        pytest.fail("Admitted before crash recovery")
    with exclusive(installation, [state]):
        (installation / "maintenance.json").unlink()
    with activity(state):
        pass


def test_child_process_activity_blocks_update_until_exit(registered):
    installation, state = registered
    script = """
import sys
from pathlib import Path
from bubo.update_admission import activity
with activity(Path(sys.argv[1])):
    print('ready', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(UpdateBusy), exclusive(installation, [state]):
            pytest.fail("Updated over another process")
    finally:
        child.communicate("done\n", timeout=5)
    assert child.returncode == 0
    with exclusive(installation, [state]):
        pass


def test_failed_multiroot_acquisition_releases_all_locks(registered, tmp_path):
    installation, state = registered
    missing = tmp_path / "z-missing-state"
    with pytest.raises(FileNotFoundError), exclusive(installation, [state, missing]):
        pytest.fail("Acquired unknown state")
    with exclusive(installation, [state]):
        pass  # registry, installation and first root were all released


def test_worker_inherits_lease_until_exit_even_after_parent_operation_ends(registered):
    installation, state = registered
    with activity(state):
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.readline()"],
            pass_fds=handoff_fds(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert child.stdout.readline().strip() == "ready"
    try:
        with pytest.raises(UpdateBusy), exclusive(installation, [state]):
            pytest.fail("Parent exit released detached worker's lease")
    finally:
        child.communicate("done\n", timeout=5)
    with exclusive(installation, [state]):
        pass


def test_double_forked_writer_retains_lease_after_ancestry_is_lost(registered):
    installation, state = registered
    ready = state / "orphan-ready"
    release = state / "orphan-release"
    script = """
import os, sys, time
from pathlib import Path
if os.fork():
    os._exit(0)
os.setsid()
if os.fork():
    os._exit(0)
Path(sys.argv[1]).write_text('ready')
deadline = time.monotonic() + 10
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
os._exit(0)
"""
    with activity(state):
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(ready), str(release)],
            pass_fds=handoff_fds(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        child.wait(timeout=5)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        # The intermediate parents have exited and setsid escaped their group.
        # Shared file descriptions, not ancestry, still block the updater.
        with pytest.raises(UpdateBusy), exclusive(installation, [state]):
            pytest.fail("Orphaned writer lost its update admission lease")
    finally:
        release.touch()
    deadline = time.monotonic() + 5
    while True:
        try:
            with exclusive(installation, [state]):
                break
        except UpdateBusy:
            assert time.monotonic() < deadline
            time.sleep(0.02)


def test_reviewer_retains_lease_when_admitted_worker_is_killed(registered):
    installation, state = registered
    ready = state / "reviewer-ready"
    release = state / "reviewer-release"
    reviewer = """
import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text('ready')
deadline = time.monotonic() + 10
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
"""
    worker_script = """
import sys
from pathlib import Path
from bubo.update_admission import activity
from bubo.subproc import run_bounded
with activity(Path(sys.argv[1])):
    run_bounded([sys.executable, '-c', sys.argv[2], sys.argv[3], sys.argv[4]])
"""
    worker = subprocess.Popen(
        [sys.executable, "-c", worker_script, str(state), reviewer, str(ready), str(release)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert worker.poll() is None
            time.sleep(0.02)
        assert ready.exists()
        worker.kill()
        worker.wait(timeout=5)
        with pytest.raises(UpdateBusy), exclusive(installation, [state]):
            pytest.fail("Surviving reviewer lost its killed worker's lease")
    finally:
        release.touch()
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
    deadline = time.monotonic() + 5
    while True:
        try:
            with exclusive(installation, [state]):
                break
        except UpdateBusy:
            assert time.monotonic() < deadline
            time.sleep(0.02)


@pytest.mark.parametrize(("value", "expected"), [(None, True), (True, True), (False, False)])
def test_auto_update_config_is_independent_boolean(value, expected):
    from bubo.review_config import review_config_from_dict

    data = {} if value is None else {"auto_update": value}
    assert review_config_from_dict(data).auto_update is expected


@pytest.mark.parametrize("value", ["false", "true", 0, 1, []])
def test_invalid_auto_update_config_rejected(value):
    from bubo.config_values import ConfigError
    from bubo.review_config import review_config_from_dict

    with pytest.raises(ConfigError):
        review_config_from_dict({"auto_update": value})


def test_environment_barrier_uses_existing_file_without_fcntl(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from bubo import update_admission

    configuration = tmp_path / "pyvenv.cfg"
    configuration.write_text("home = existing-python\n")
    configuration.chmod(0o444)
    calls = []

    @contextmanager
    def lock(path, *, exclusive):
        calls.append((path, exclusive))
        with path.open("rb") as stream:
            yield stream

    monkeypatch.setattr(update_admission, "fcntl", None)
    monkeypatch.setattr(update_admission, "_file_lock", lock)
    with update_admission.environment_lock(tmp_path) as stream:
        assert stream.read() == b"home = existing-python\n"
    with update_admission.environment_lock(tmp_path, exclusive=True):
        pass
    assert calls == [(configuration, False), (configuration, True)]
    assert list(tmp_path.iterdir()) == [configuration]
    assert configuration.read_text() == "home = existing-python\n"
