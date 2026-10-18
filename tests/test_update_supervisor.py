import json
import os
import signal
import sys
import time
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from bubo import update_helper, update_supervisor
from bubo.update_admission import UpdateBusy, _file_lock
from bubo.update_identity import atomic_json
from bubo.update_supervisor import ResidentSupervisor


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def supervisor(tmp_path, script, *args):
    result = ResidentSupervisor(
        tmp_path, tmp_path / "state", "service", [sys.executable, "-c", script, *map(str, args)]
    )
    result.maintenance = True
    result._launch()
    return result


def test_exit_observation_retains_owned_pid_until_group_stop(tmp_path):
    parent = supervisor(tmp_path, "raise SystemExit(7)")
    child = parent.child
    try:
        wait_until(lambda: child.poll() is not None)
        assert child.poll() == 7
        assert child.returncode is None
        assert os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT).si_status == 7
        assert parent._stop()
        assert child.returncode == 7
    finally:
        if parent.child:
            parent._stop()


def test_already_reaped_child_is_never_signalled(tmp_path, monkeypatch):
    parent = supervisor(tmp_path, "pass")
    parent.child.wait(timeout=5)
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    assert not parent._stop()
    assert signals == []


def test_replacement_uses_original_directory_path_after_source_swap(tmp_path, monkeypatch):
    from pathlib import Path

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    marker = tmp_path / "child-cwd"
    parent = ResidentSupervisor(
        tmp_path,
        tmp_path / "state",
        "service",
        [
            sys.executable,
            "-c",
            "import os, sys, time; from pathlib import Path; Path(sys.argv[1]).write_text(os.getcwd()); time.sleep(60)",
            str(marker),
        ],
    )
    parent.maintenance = True
    checkout.rename(tmp_path / "previous-checkout")
    checkout.mkdir()
    # Parent cwd still refers to the previous checkout inode; replacement must
    # resolve the original pathname again and run in the newly activated source.
    assert Path.cwd().name == "previous-checkout"
    try:
        parent._launch()
        wait_until(marker.exists)
        assert marker.read_text() == str(checkout)
    finally:
        assert parent._stop()


def test_same_group_descendant_is_stopped_after_leader_exits(tmp_path):
    marker = tmp_path / "descendant"
    script = """
import subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
"""
    parent = supervisor(tmp_path, script, marker)
    try:
        wait_until(marker.exists)
        wait_until(lambda: parent.child.poll() is not None)
        assert parent.child.returncode is None
        assert parent._stop()
        row = update_supervisor._processes().get(int(marker.read_text()))
        assert row is None or row[2].startswith("Z")
    finally:
        if parent.child:
            parent._stop()


def test_escaped_descendant_defers_and_resumes_original_group(tmp_path):
    marker = tmp_path / "escaped"
    cleaned = tmp_path / "cleaned"
    script = """
import signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
def clean(*args):
    child.terminate()
    child.wait(timeout=5)
    Path(sys.argv[2]).write_text('done')
signal.signal(signal.SIGUSR1, clean)
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    parent = supervisor(tmp_path, script, marker, cleaned)
    try:
        wait_until(marker.exists)
        assert not parent._stop()
        assert parent.child.poll() is None
        # The still-owned parent cleans its detached child; no numeric orphan
        # PID is signalled by the test or the update controller.
        parent.child.send_signal(signal.SIGUSR1)
        wait_until(cleaned.exists)
        assert parent._stop()
    finally:
        if parent.child:
            parent.child.send_signal(signal.SIGUSR1)
            wait_until(cleaned.exists)
            parent._stop()


def test_delegate_execs_retained_archive_without_persisting_credentials(tmp_path, monkeypatch):
    from bubo import paths

    state = tmp_path / "state"
    state.mkdir()
    archive = tmp_path / "helper.pyz"
    archive.touch()
    atomic_json(state / "update-registration.json", {"installation": str(tmp_path), "ready": False})
    atomic_json(
        tmp_path / "installation.json",
        {
            "archive": str(archive),
            "python": sys.executable,
            "protocol_version": 1,
            "owner": os.getuid(),
        },
    )
    monkeypatch.delenv(update_supervisor.CHILD_ENV)
    monkeypatch.setenv("TEST_ENV_ONLY_CREDENTIAL", "fixture-secret")
    monkeypatch.setattr(paths, "DB", state / "reviewer.sqlite")
    monkeypatch.setattr(update_helper, "ensure_registered", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys, "orig_argv", ["python", "-m", "bubo.poller", "service", "start", "--foreground"]
    )
    calls = []
    monkeypatch.setattr(os, "execve", lambda *args: calls.append(args))
    update_supervisor.delegate("service")
    executable, args, env = calls[0]
    assert executable == sys.executable
    assert str(archive) in args
    assert args[-2:] == ["supervise", "service"]
    assert "fixture-secret" not in repr(args)
    assert env["TEST_ENV_ONLY_CREDENTIAL"] == "fixture-secret"
    assert json.loads(env[update_supervisor.COMMAND_ENV]) == [sys.executable, *sys.orig_argv[1:]]
    assert all("fixture-secret" not in path.read_text() for path in tmp_path.rglob("*.json"))


def test_missing_parent_blocks_control_without_signalling_any_pid(tmp_path, monkeypatch):
    directory = tmp_path / "supervisors" / "parent"
    entry = {"pid": 123, "identity": "stale", "state": str(tmp_path), "kind": "service"}
    atomic_json(directory / "owner.json", entry)
    entry["directory"] = str(directory)
    monkeypatch.setattr(update_supervisor, "_alive", lambda _: False)
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    controller = update_supervisor.ResidentController(tmp_path)
    assert not controller.stop({"resident_supervisors": [entry]})
    assert signals == []
    assert not (directory / "command.json").exists()


def test_missing_archive_keeps_original_runtime_and_blocks_replacement(tmp_path, monkeypatch):
    from bubo import paths

    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "activity.lock").touch()
    atomic_json(state / "update-registration.json", {"installation": str(tmp_path)})
    atomic_json(tmp_path / "installation.json", {"protocol_version": 1, "owner": os.getuid()})
    monkeypatch.delenv(update_supervisor.CHILD_ENV)
    monkeypatch.setattr(paths, "DB", state / "reviewer.sqlite")
    monkeypatch.setattr(update_helper, "ensure_registered", lambda *args, **kwargs: None)
    with ExitStack() as leases:
        monkeypatch.setattr(update_supervisor, "_fallback_leases", leases)
        update_supervisor.delegate("service")
        try:
            with _file_lock(tmp_path / "activity.lock", exclusive=True):
                raise AssertionError("Unmanaged runtime did not pin its installation")
        except UpdateBusy:
            pass
    with _file_lock(tmp_path / "activity.lock", exclusive=True):
        pass


def test_delayed_stop_delivery_is_rechecked_before_accepting_census(tmp_path, monkeypatch):
    parent = ResidentSupervisor(tmp_path, tmp_path / "state", "service", [])
    parent.child = SimpleNamespace(pid=10)
    tables = iter(
        [
            {10: (1, 10, "R"), 11: (10, 10, "R")},
            {10: (1, 10, "T"), 11: (10, 10, "T")},
            {10: (1, 10, "T"), 11: (10, 10, "T")},
        ]
    )
    monkeypatch.setattr(update_supervisor, "_processes", lambda: next(tables))
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    assert parent._frozen_group()
    assert signals == [(10, signal.SIGSTOP)]


def test_descendant_escaping_during_stop_delivery_rejects_census(tmp_path, monkeypatch):
    parent = ResidentSupervisor(tmp_path, tmp_path / "state", "service", [])
    parent.child = SimpleNamespace(pid=10)
    tables = iter(
        [
            {10: (1, 10, "R"), 11: (10, 10, "R")},
            {10: (1, 10, "T"), 11: (10, 11, "S")},
        ]
    )
    monkeypatch.setattr(update_supervisor, "_processes", lambda: next(tables))
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    assert not parent._frozen_group()
    assert signals == [(10, signal.SIGSTOP)]


def pending_parent(tmp_path, monkeypatch):
    parent = ResidentSupervisor(tmp_path, tmp_path / "state", "service", ["bubo", "serve"])
    parent.state.mkdir()
    (tmp_path / "activity.lock").touch()
    (parent.state / "update-activity.lock").touch()
    old = {**parent.record, "pid": 987654321, "identity": "old-parent", "child_state": "stopped"}
    entry = {**old, "directory": str(parent.directory)}
    journal = {
        "attempt_id": "attempt",
        "token": "token",
        "states": [str(parent.state)],
        "resident_supervisors": [entry],
    }
    atomic_json(tmp_path / "attempt.json", journal)
    atomic_json(tmp_path / "maintenance.json", {"attempt_id": "attempt", "token": "token"})
    atomic_json(parent.directory / "owner.json", old)
    monkeypatch.setattr(update_supervisor, "_alive", lambda record: record["pid"] == os.getpid())
    monkeypatch.setattr(update_supervisor, "_processes", lambda: {})
    return parent, old, journal


def test_reattach_claims_exact_role_without_launching_or_replaying_mailbox(tmp_path, monkeypatch):
    parent, old, journal = pending_parent(tmp_path, monkeypatch)
    atomic_json(parent.directory / "command.json", {"request_id": "stale-start", "action": "start"})
    assert parent._reattach()
    assert parent.child is None
    assert parent.maintenance
    assert parent.last_request == "stale-start"
    owner = update_supervisor._read(parent.directory / "owner.json")
    assert owner["replacement"] == {
        "pid": old["pid"],
        "identity": old["identity"],
        "attempt_id": "attempt",
        "token": "token",
    }
    assert update_supervisor._read(tmp_path / "attempt.json") == journal


def test_reattach_rejects_changed_launch_and_ambiguous_roles(tmp_path, monkeypatch):
    parent, old, journal = pending_parent(tmp_path, monkeypatch)
    parent.record["launch_digest"] = "different-command-or-cwd"
    assert not parent._reattach()
    parent.record["launch_digest"] = old["launch_digest"]
    journal["resident_supervisors"] *= 2
    atomic_json(tmp_path / "attempt.json", journal)
    assert not parent._reattach()


def test_reattach_rejects_unpublished_spawn_and_existing_group(tmp_path, monkeypatch):
    parent, old, _journal = pending_parent(tmp_path, monkeypatch)
    old["child_state"] = "launching"
    atomic_json(parent.directory / "owner.json", old)
    assert not parent._reattach()
    old["child"] = {"pid": 12345, "identity": "dead-child"}
    atomic_json(parent.directory / "owner.json", old)
    monkeypatch.setattr(update_supervisor, "_processes", lambda: {456: (1, 12345, "S")})
    assert not parent._reattach()
    monkeypatch.setattr(update_supervisor, "_processes", lambda: {})
    assert parent._reattach()


def test_reattach_rejects_live_parent_and_detached_writer(tmp_path, monkeypatch):
    import pytest

    parent, old, _journal = pending_parent(tmp_path, monkeypatch)
    monkeypatch.setattr(update_supervisor, "_alive", lambda record: True)
    assert not parent._reattach()
    monkeypatch.setattr(update_supervisor, "_alive", lambda record: False)
    with _file_lock(parent.state / "update-activity.lock", exclusive=False):
        with pytest.raises(UpdateBusy):
            parent._reattach()
    assert update_supervisor._read(parent.directory / "owner.json") == old


def test_controller_accepts_dead_quiescent_parent_stop_only(tmp_path, monkeypatch):
    import pytest

    _parent, _old, journal = pending_parent(tmp_path, monkeypatch)
    controller = update_supervisor.ResidentController(tmp_path, timeout=0)
    entry = journal["resident_supervisors"][0]
    assert controller._request(entry, journal, "stop")
    with pytest.raises(update_supervisor.ResidentRestartRequired, match="service_restart_required"):
        controller._request(entry, journal, "start", "restored")


def test_controller_adopts_published_replacement_before_command(tmp_path, monkeypatch):
    import pytest

    parent, old, journal = pending_parent(tmp_path, monkeypatch)
    assert parent._reattach()
    controller = update_supervisor.ResidentController(tmp_path, timeout=0)
    with pytest.raises(TimeoutError):
        controller._request(journal["resident_supervisors"][0], journal, "stop")
    persisted = update_supervisor._read(tmp_path / "attempt.json")
    assert persisted["resident_supervisors"][0]["pid"] == os.getpid()
    assert persisted["resident_supervisors"][0]["replacement"]["identity"] == old["identity"]


def test_replacement_rejects_command_addressed_to_old_parent(tmp_path, monkeypatch):
    import pytest

    parent, old, _journal = pending_parent(tmp_path, monkeypatch)
    assert parent._reattach()
    request = {
        "attempt_id": "attempt",
        "token": "token",
        "action": "stop",
        "parent": {key: old[key] for key in ("pid", "identity")},
    }
    with pytest.raises(update_supervisor.StartupRejected, match="parent changed"):
        parent._handle(request)
    request["parent"] = {key: parent.record[key] for key in ("pid", "identity")}
    assert parent._handle(request)


def test_reattach_rejects_changed_decision(tmp_path, monkeypatch):
    parent, _old, _journal = pending_parent(tmp_path, monkeypatch)
    atomic_json(tmp_path / "maintenance.json", {"attempt_id": "another", "token": "token"})
    assert not parent._reattach()


def test_replacement_can_be_replaced_before_and_after_adoption(tmp_path, monkeypatch):
    parent, _old, journal = pending_parent(tmp_path, monkeypatch)
    assert parent._reattach()
    monkeypatch.setattr(update_supervisor, "_alive", lambda record: False)
    second = ResidentSupervisor(tmp_path, parent.state, "service", ["bubo", "serve"])
    assert second._reattach()
    # Simulate durable controller adoption, followed by loss of that parent.
    journal["resident_supervisors"][0].update(second.record)
    atomic_json(tmp_path / "attempt.json", journal)
    third = ResidentSupervisor(tmp_path, parent.state, "service", ["bubo", "serve"])
    assert third._reattach()
    assert third.child is None
    assert third.record["replacement"]["identity"] == second.record["identity"]


def test_reattached_run_ignores_old_start_and_keeps_fence_on_cancel(tmp_path, monkeypatch):
    parent, old, _journal = pending_parent(tmp_path, monkeypatch)
    (tmp_path / "registry.lock").touch()
    atomic_json(
        parent.directory / "command.json",
        {
            "request_id": "old-start",
            "action": "start",
            "attempt_id": "attempt",
            "token": "token",
            "parent": {key: old[key] for key in ("pid", "identity")},
            "outcome": "restored",
        },
    )
    monkeypatch.setattr(
        parent,
        "_launch",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("must not launch before new command")
        ),
    )
    monkeypatch.setattr(
        update_supervisor.time, "sleep", lambda _: setattr(parent, "cancelled", True)
    )
    assert parent.run() == 128 + signal.SIGTERM
    assert parent.child is None
    assert (tmp_path / "maintenance.json").exists()
    assert update_supervisor._read(parent.directory / "owner.json")["closed"]
    assert not (parent.directory / "response.json").exists()


def test_real_fresh_parent_waits_for_controller_adoption(tmp_path):
    import subprocess

    state = tmp_path / "state"
    state.mkdir()
    for path in (
        tmp_path / "activity.lock",
        tmp_path / "registry.lock",
        state / "update-activity.lock",
    ):
        path.touch()
    command = [sys.executable, "-c", "raise SystemExit(99)"]
    previous = ResidentSupervisor(tmp_path, state, "service", command)
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        previous.record.update(pid=dead.pid, identity=update_supervisor._start_identity(dead.pid))
        previous._publish()
    finally:
        dead.terminate()
        dead.wait(timeout=5)
    entry = {**previous.record, "directory": str(previous.directory)}
    journal = {
        "attempt_id": "attempt",
        "token": "token",
        "states": [str(state)],
        "resident_supervisors": [entry],
    }
    atomic_json(tmp_path / "attempt.json", journal)
    atomic_json(tmp_path / "maintenance.json", {"attempt_id": "attempt", "token": "token"})
    script = """
import json, sys
from pathlib import Path
from bubo.update_supervisor import ResidentSupervisor
raise SystemExit(ResidentSupervisor(Path(sys.argv[1]), Path(sys.argv[2]), 'service',
                                   json.loads(sys.argv[3])).run())
"""
    fresh = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path), str(state), json.dumps(command)]
    )
    try:
        wait_until(
            lambda: update_supervisor._read(previous.directory / "owner.json")["pid"] == fresh.pid
        )
        owner = update_supervisor._read(previous.directory / "owner.json")
        assert "child" not in owner
        controller = update_supervisor.ResidentController(tmp_path, timeout=5)
        assert controller._request(entry, journal, "stop")
        assert (
            update_supervisor._read(tmp_path / "attempt.json")["resident_supervisors"][0]["pid"]
            == fresh.pid
        )
        assert fresh.poll() is None
        assert "child" not in update_supervisor._read(previous.directory / "owner.json")
    finally:
        fresh.terminate()
        fresh.wait(timeout=5)
    assert (tmp_path / "maintenance.json").exists()


def test_unproven_orphan_never_requests_fresh_credentials(tmp_path, monkeypatch):
    import pytest

    parent, old, journal = pending_parent(tmp_path, monkeypatch)
    old["child_state"] = "launching"
    atomic_json(parent.directory / "owner.json", old)
    controller = update_supervisor.ResidentController(tmp_path, timeout=0)
    with pytest.raises(OSError, match="unavailable") as failure:
        controller._request(journal["resident_supervisors"][0], journal, "start", "restored")
    assert not isinstance(failure.value, update_supervisor.ResidentRestartRequired)


def test_controller_rejects_changed_launch_role_even_for_live_parent(tmp_path, monkeypatch):
    import pytest

    parent, old, journal = pending_parent(tmp_path, monkeypatch)
    old["launch_digest"] = "changed"
    atomic_json(parent.directory / "owner.json", old)
    monkeypatch.setattr(update_supervisor, "_alive", lambda record: True)
    controller = update_supervisor.ResidentController(tmp_path, timeout=0)
    with pytest.raises(OSError, match="unavailable"):
        controller._request(journal["resident_supervisors"][0], journal, "start", "restored")
    assert not (parent.directory / "command.json").exists()


@pytest.mark.parametrize("kind", ["service", "mcp-http", "mcp-stdio"])
def test_legacy_windows_launch_keeps_client_visible_process(monkeypatch, kind):
    leases = []

    def replace_process(*args, **kwargs):
        pytest.fail("legacy Windows launch must not exit or replace its client-visible process")

    monkeypatch.delenv(update_supervisor.CHILD_ENV, raising=False)
    monkeypatch.setattr(
        update_supervisor,
        "os",
        SimpleNamespace(
            name="nt",
            environ=os.environ,
            execve=replace_process,
            _exit=replace_process,
        ),
    )
    monkeypatch.setattr(update_helper, "ensure_registered", replace_process)
    monkeypatch.setattr(update_supervisor, "_pin_windows_legacy", lambda: leases.append("held"))
    assert update_supervisor.delegate(kind) is None
    assert leases == ["held"]


@pytest.mark.parametrize("registration_races_startup", [False, True])
def test_windows_legacy_lease_hands_off_bridge_to_registered_admission(
    tmp_path, monkeypatch, registration_races_startup
):
    from contextlib import ExitStack, contextmanager

    from bubo import update_admission, update_supervisor
    from bubo.update_admission import UpdateBusy, _file_lock
    from bubo.update_identity import atomic_json

    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "pyvenv.cfg").touch()
    installation = tmp_path / "installation"
    installation.mkdir()
    (installation / "activity.lock").touch()
    metadata = installation / "installation.json"
    bridge_events = []

    def register():
        atomic_json(metadata, {"environment": str(environment)})

    @contextmanager
    def bridge(*args, **kwargs):
        bridge_events.append("acquired")
        register()
        try:
            yield
        finally:
            bridge_events.append("released")

    if not registration_races_startup:
        register()
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr("bubo.update_helper.installation_for_environment", lambda _: installation)
    monkeypatch.setattr(update_admission, "environment_lock", bridge)
    with ExitStack() as lifetime:
        monkeypatch.setattr(update_supervisor, "_fallback_leases", lifetime)
        update_supervisor._pin_windows_legacy()
        assert bridge_events == (["acquired", "released"] if registration_races_startup else [])
        with pytest.raises(UpdateBusy), _file_lock(installation / "activity.lock", exclusive=True):
            pytest.fail("Legacy lifetime lease did not exclude replacement")
    with _file_lock(installation / "activity.lock", exclusive=True):
        pass


def test_windows_legacy_start_during_maintenance_releases_admission(tmp_path, monkeypatch):
    from bubo import update_supervisor
    from bubo.update_admission import MaintenanceInProgress, _file_lock
    from bubo.update_identity import atomic_json

    installation = tmp_path / "installation"
    installation.mkdir()
    (installation / "activity.lock").touch()
    atomic_json(installation / "installation.json", {"environment": sys.prefix})
    atomic_json(installation / "maintenance.json", {"token": "fixture"})
    monkeypatch.setattr("bubo.update_helper.installation_for_environment", lambda _: installation)
    with pytest.raises(MaintenanceInProgress):
        update_supervisor._pin_windows_legacy()
    with _file_lock(installation / "activity.lock", exclusive=True):
        pass
