"""Native Windows containment checks; no installed Bubo environment is mutated."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bubo.update_platform import windows_process_identity
from bubo.update_windows_process import new_job_name, recover_job, run_capture

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows Job Objects"
)


def test_native_job_captures_output_and_passes_credentials_only_in_memory():
    name = new_job_name()
    try:
        result = run_capture(
            [sys.executable, "-c", "import os; print(os.environ['BUBO_TEST_JOB_SECRET'])"],
            name=name,
            env={**os.environ, "BUBO_TEST_JOB_SECRET": "synthetic-in-memory-value"},
            timeout=10,
        )
        assert result.strip() == b"synthetic-in-memory-value"
        recover_job(name)
    finally:
        recover_job(name)


def test_native_root_exit_cannot_hide_live_descendant(tmp_path):
    name = new_job_name()
    receipt = tmp_path / "descendant.json"
    script = (
        "import subprocess, sys\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(receipt)!r}).write_text(str(child.pid))\n"
    )
    try:
        with pytest.raises(TimeoutError, match="descendants"):
            run_capture([sys.executable, "-c", script], name=name, env=dict(os.environ), timeout=10)
        assert windows_process_identity(int(receipt.read_text())) is None
    finally:
        recover_job(name)


def test_native_coordinator_death_terminates_child_and_grandchild(tmp_path):
    name = new_job_name()
    receipt = tmp_path / "descendants.json"
    child_script = (
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(receipt)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(60)\n"
    )
    controller_script = (
        "import msvcrt, os, sys\n"
        "from bubo.update_windows_process import JobProcess\n"
        "with open(os.devnull, 'rb') as stdin, open(os.devnull, 'wb') as output:\n"
        f'    child = JobProcess([sys.executable, "-c", {child_script!r}], name={name!r},\n'
        "        handles=(msvcrt.get_osfhandle(stdin.fileno()),\n"
        "                 msvcrt.get_osfhandle(output.fileno()),\n"
        "                 msvcrt.get_osfhandle(output.fileno())), env=dict(os.environ))\n"
        "    child.wait(timeout=60)\n"
    )
    controller = subprocess.Popen(
        [sys._base_executable, "-c", controller_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        deadline = time.monotonic() + 10
        while not receipt.exists() and time.monotonic() < deadline:
            assert controller.poll() is None
            time.sleep(0.02)
        assert receipt.exists()
        # Wait for complete publication rather than observing a newly opened empty file.
        while True:
            try:
                pids = json.loads(receipt.read_text())
                break
            except json.JSONDecodeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        identities = {pid: windows_process_identity(pid) for pid in pids}
        assert all(identities.values())
        controller.kill()
        controller.communicate(timeout=10)
        recover_job(name, timeout=10)
        assert all(
            windows_process_identity(pid) != identity for pid, identity in identities.items()
        )
    finally:
        if controller.poll() is None:
            controller.kill()
        controller.communicate(timeout=10)
        recover_job(name, timeout=10)


def test_native_guardian_retains_job_after_parent_detaches(tmp_path):
    import msvcrt

    from bubo.update_windows_process import JobProcess

    name = new_job_name()
    receipt = tmp_path / "guardian.json"
    child_script = (
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        "from bubo.update_windows_process import GuardianJob\n"
        f"guardian = GuardianJob({name!r})\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(receipt)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(60)\n"
    )
    process = None
    try:
        with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as output:
            process = JobProcess(
                [sys._base_executable, "-c", child_script],
                name=name,
                handles=tuple(
                    msvcrt.get_osfhandle(stream.fileno())
                    for stream in (
                        stdin,
                        output,
                        output,
                    )
                ),
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            )
        deadline = time.monotonic() + 10
        while True:
            assert process.poll() is None
            try:
                pids = json.loads(receipt.read_text())
                break
            except FileNotFoundError, json.JSONDecodeError:
                assert time.monotonic() < deadline, "Guardian did not acquire its Job handle"
                time.sleep(0.02)
        identities = {pid: windows_process_identity(pid) for pid in pids}
        assert all(identities.values())

        assert process.identity() == identities[process.pid]

        def acknowledged(actual):
            assert pids[0] == actual.pid
            assert windows_process_identity(actual.pid) == identities[actual.pid]

        process.detach(acknowledged)
        process.close()
        assert all(
            windows_process_identity(pid) == identity for pid, identity in identities.items()
        )
        # No parent Job handle remains. Killing the guardian closes the last Job
        # handle, and must stop its descendant without an updater recovery call.
        import ctypes as C

        api = C.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [C.c_uint32, C.c_int32, C.c_uint32]
        api.OpenProcess.restype = C.c_void_p
        api.TerminateProcess.argtypes = [C.c_void_p, C.c_uint32]
        api.TerminateProcess.restype = C.c_int32
        api.CloseHandle.argtypes = [C.c_void_p]
        handle = api.OpenProcess(1, False, pids[0])
        assert handle
        try:
            assert api.TerminateProcess(handle, 1)
        finally:
            api.CloseHandle(handle)
        deadline = time.monotonic() + 10
        while any(
            windows_process_identity(pid) == identity for pid, identity in identities.items()
        ):
            assert time.monotonic() < deadline, "Guardian death left a descendant alive"
            time.sleep(0.02)
    finally:
        if process:
            process.close()
        recover_job(name, timeout=10)


@pytest.mark.parametrize("parent_launcher", [False, True, "broker"])
def test_native_retained_guardian_holds_admission_after_worker_root_exits(
    tmp_path, parent_launcher
):
    import msvcrt

    from bubo.update_admission import _PROCESS_VERSION, environment_lock
    from bubo.update_helper import _bundle
    from bubo.update_locking import lock_file
    from bubo.update_platform import current_owner
    from bubo.update_windows_jobs import JobOwnership
    from bubo.update_windows_process import JobProcess
    from bubo.update_windows_worker import spawn

    installation, state, environment = [
        tmp_path / item for item in ("installation", "state", "venv")
    ]
    for directory in (installation, state, environment):
        directory.mkdir()
    admission_paths = [
        environment / "pyvenv.cfg",
        installation / "activity.lock",
        state / "update-activity.lock",
    ]
    for path in admission_paths:
        path.touch()
    generation = {"version": _PROCESS_VERSION, "attempt": "native-guardian"}
    archive = _bundle(installation, Path(sys.executable))
    (installation / "generation.json").write_text(json.dumps(generation))
    (installation / "installation.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "owner": current_owner(),
                "environment": str(environment),
                "states": [str(state)],
                "python": sys._base_executable,
                "archive": str(archive),
            }
        )
    )
    (state / "update-registration.json").write_text(
        json.dumps(
            {
                "installation": str(installation),
                "ready": True,
            }
        )
    )
    owner = None if parent_launcher else JobOwnership(installation, worker=True)
    root_file, gate = tmp_path / "worker-root.json", tmp_path / "release-descendant"
    descendant = (
        "from pathlib import Path\nimport time\n"
        f"while not Path({str(gate)!r}).exists(): time.sleep(0.02)\n"
    )
    command = (
        "import json, os, subprocess, sys\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {descendant!r}])\n"
        f"Path({str(root_file)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "print('worker output survives handoff', flush=True)\n"
    )
    payload = {
        "installation": str(installation),
        "state": str(state),
        "environment": str(environment),
        "generation": generation,
        "parent": {**owner.record, "job_created": True} if owner else {},
        "args": [sys.executable, "-c", command, "--worker", str(tmp_path / "review.json")],
    }
    code = "import sys; sys.path.insert(0, sys.argv[1]); from bubo.update_windows_worker import main; main(sys.argv[2])"
    process = None
    resident = server = None
    job_name = owner.name if owner else None
    output_path = tmp_path / "worker-output.log"
    try:
        with open(os.devnull, "rb") as stdin, output_path.open("wb") as output:
            if parent_launcher:
                if parent_launcher == "broker":
                    from bubo.update_windows_broker import WorkerBroker

                    server = WorkerBroker(installation, state)
                    launched = tmp_path / "broker-result.json"
                    resident_script = (
                        "import json, sys, time\nfrom pathlib import Path\n"
                        "from bubo.update_windows_broker import spawn_via_broker\n"
                        f"pid = spawn_via_broker({payload['args']!r}, stdout=sys.stdout)\n"
                        f"assert spawn_via_broker({payload['args']!r}, stdout=sys.stdout) == pid\n"
                        f"Path({str(launched)!r}).write_text(json.dumps(pid))\n"
                        "time.sleep(60)\n"
                    )
                    resident = JobProcess(
                        [sys.executable, "-c", resident_script],
                        name=new_job_name(),
                        handles=tuple(
                            msvcrt.get_osfhandle(stream.fileno())
                            for stream in (stdin, output, output)
                        ),
                        env={
                            **os.environ,
                            **server.environment(),
                            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                        },
                    )
                    server.bind(resident.job)
                    deadline = time.monotonic() + 15
                    while True:
                        assert resident.poll() is None
                        try:
                            guardian_pid = json.loads(launched.read_text())
                            break
                        except FileNotFoundError, json.JSONDecodeError:
                            assert time.monotonic() < deadline, (
                                "Resident broker did not acknowledge"
                            )
                            time.sleep(0.02)
                    # Detached worker admission and descendants must outlive both
                    # the resident tree and its now-unneeded broker endpoint.
                    server.close()
                    resident.close()
                    resident = None
                else:
                    guardian_pid = spawn(payload["args"], installation, state, stdout=output)
                    assert (
                        spawn(payload["args"], installation, state, stdout=output) == guardian_pid
                    )
                receipts = list((installation / "windows-jobs").glob("*.json"))
                assert len(receipts) == 1
                owner_path = receipts[0]
                job_name = json.loads(owner_path.read_text())["job"]
            else:
                process = JobProcess(
                    [
                        sys._base_executable,
                        "-I",
                        "-S",
                        "-c",
                        code,
                        archive,
                        "GUARDIAN_TEST_PAYLOAD",
                    ],
                    name=owner.name,
                    on_job_created=owner.created,
                    handles=tuple(
                        msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, output, output)
                    ),
                    env={**os.environ, "GUARDIAN_TEST_PAYLOAD": json.dumps(payload)},
                )
                guardian_pid, owner_path = process.pid, owner.path
        guardian_start = windows_process_identity(guardian_pid)
        assert guardian_start
        deadline = time.monotonic() + 10
        while True:
            assert windows_process_identity(guardian_pid) == guardian_start
            try:
                root_pid, descendant_pid = json.loads(root_file.read_text())
                if windows_process_identity(root_pid) is None:
                    break
            except FileNotFoundError, json.JSONDecodeError:
                pass
            assert time.monotonic() < deadline, "Worker root did not exit"
            time.sleep(0.02)
        assert windows_process_identity(descendant_pid)
        if process:
            assert process.identity() == guardian_start
            owner.handoff(process)
        with environment_lock(environment, exclusive=True):
            pass  # Registered worker is protected by installation/state leases below.
        for path in admission_paths[1:]:
            with path.open("rb") as stream, pytest.raises(BlockingIOError):
                lock_file(stream.fileno(), exclusive=True)
        gate.touch()
        deadline = time.monotonic() + 10
        while owner_path.exists() or windows_process_identity(guardian_pid) == guardian_start:
            assert time.monotonic() < deadline, "Guardian failed to retire after descendant exit"
            time.sleep(0.02)
        assert not owner_path.with_suffix(".ack").exists()
        assert "worker output survives handoff" in output_path.read_text()
        with environment_lock(environment, exclusive=True):
            pass
        for path in admission_paths[1:]:
            with path.open("rb") as stream:
                lock_file(stream.fileno(), exclusive=True)
    finally:
        if server:
            server.close()
        if resident:
            resident.close()
        if process:
            process.close()
        if job_name:
            recover_job(job_name, timeout=10)


def test_native_read_only_environment_lock_blocks_first_registration(tmp_path):
    from bubo.update_admission import UpdateBusy, environment_lock

    environment = tmp_path / "environment"
    environment.mkdir()
    configuration = environment / "pyvenv.cfg"
    configuration.write_text("home = native-read-only-fixture\n")
    configuration.chmod(0o444)
    ready, release = tmp_path / "ready", tmp_path / "release"
    script = (
        "import time\nfrom pathlib import Path\n"
        "from bubo.update_admission import environment_lock\n"
        f"with environment_lock(Path({str(environment)!r})):\n"
        f"    Path({str(ready)!r}).touch()\n"
        f"    while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.02)
        assert ready.exists()
        with pytest.raises(UpdateBusy), environment_lock(environment, exclusive=True):
            pytest.fail("Registration bypassed a native shared activity lock")
    finally:
        release.touch()
        try:
            child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=10)
    with environment_lock(environment, exclusive=True):
        pass
    assert configuration.read_text() == "home = native-read-only-fixture\n"
    assert list(environment.iterdir()) == [configuration]


@pytest.mark.parametrize("public_wrapper", [False, True])
def test_native_bounded_guardian_preserves_batch_command_contract(
    tmp_path, monkeypatch, public_wrapper
):
    from bubo.update_windows_bounded import run_bounded_windows

    run = run_bounded_windows
    installation = None
    if public_wrapper:
        from bubo import paths
        from bubo.subproc import run_bounded
        from bubo.update_admission import _PROCESS_VERSION

        installation, state = tmp_path / "installation", tmp_path / "state"
        installation.mkdir()
        state.mkdir()
        (installation / "activity.lock").touch()
        (state / "update-activity.lock").touch()
        (installation / "generation.json").write_text(json.dumps({"version": _PROCESS_VERSION}))
        (state / "update-registration.json").write_text(
            json.dumps({"installation": str(installation)})
        )
        monkeypatch.setattr(paths, "DB", state / "bubo.db")
        run = run_bounded
    command = tmp_path / "reviewer fixture.cmd"
    command.write_text(
        "@echo off\r\necho standard output\r\necho error output 1>&2\r\nexit /b 7\r\n"
    )
    args = [str(command)]
    baseline = subprocess.run(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    result = run(args, cwd=tmp_path, timeout=10)
    assert result.returncode == baseline.returncode == 7
    assert result.stdout == baseline.stdout
    if installation:
        assert list((installation / "windows-jobs").iterdir()) == []


def test_native_windows_json_creation_and_unicode_replacement(tmp_path):
    from bubo.update_identity import atomic_json

    directory = tmp_path / "journal café 空間"
    target = directory / "attempt.json"
    atomic_json(target, {"generation": 1})
    assert json.loads(target.read_text()) == {"generation": 1}
    atomic_json(target, {"generation": 2})
    assert json.loads(target.read_text()) == {"generation": 2}
    assert list(directory.iterdir()) == [target]


def test_native_windows_locked_journal_preserves_old_record(tmp_path):
    from bubo.update_identity import atomic_json

    target = tmp_path / "attempt.json"
    atomic_json(target, {"generation": 1})
    # The CRT read handle permits reading/writing, but does not share deletion.
    with target.open("rb") as locked:
        with pytest.raises(PermissionError) as error:
            atomic_json(target, {"generation": 2})
        assert error.value.winerror in {5, 32}
        assert json.loads(locked.read()) == {"generation": 1}
    assert json.loads(target.read_text()) == {"generation": 1}
    assert list(tmp_path.iterdir()) == [target]


def test_native_bounded_command_retires_durable_job_receipt(tmp_path):
    from bubo.update_windows_bounded import run_bounded_windows
    from bubo.update_windows_jobs import drain

    result = run_bounded_windows(
        [sys.executable, "-c", 'print("ledger-ok")'],
        installation=tmp_path,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout == "ledger-ok\n"
    assert list((tmp_path / "windows-jobs").iterdir()) == []
    drain(tmp_path)


def test_native_abandoned_job_recovers_with_living_owner(tmp_path):
    import msvcrt
    from types import SimpleNamespace

    from bubo.update_windows_jobs import JobOwnership, drain
    from bubo.update_windows_process import JobProcess

    lease = JobOwnership(tmp_path)
    with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as output:
        child = JobProcess(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            name=lease.name,
            on_job_created=lease.created,
            env=dict(os.environ),
            handles=tuple(
                msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, output, output)
            ),
        )
    try:

        def failed_close():
            raise TimeoutError("cleanup unproven")

        with pytest.raises(TimeoutError):
            lease.close(SimpleNamespace(name=lease.name, close=failed_close))
        assert child.poll() is None
        drain(tmp_path)
        assert child.wait(timeout=5) != 0
        assert not lease.path.exists()
    finally:
        child.close()


def test_native_resident_output_transfer_checks_exact_process_and_job(tmp_path):
    import msvcrt

    from bubo.update_windows_process import JobProcess, duplicate_output

    receipt = tmp_path / "output-owner.json"
    output = tmp_path / "worker-output.log"
    script = (
        "import json, msvcrt, os, time\nfrom pathlib import Path\n"
        f'stream = open({str(output)!r}, "ab", buffering=0)\n'
        f"path = Path({str(receipt)!r})\n"
        'temporary = path.with_suffix(".tmp")\n'
        "temporary.write_text(json.dumps([os.getpid(), msvcrt.get_osfhandle(stream.fileno())]))\n"
        "temporary.replace(path)\n"
        "time.sleep(60)\n"
    )
    with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as sink:
        child = JobProcess(
            [sys.executable, "-c", script],
            name=new_job_name(),
            env=dict(os.environ),
            handles=tuple(msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, sink, sink)),
        )
        try:
            deadline = time.monotonic() + 10
            while not receipt.exists():
                assert child.poll() is None
                if time.monotonic() >= deadline:
                    pytest.fail("native output owner did not start")
                time.sleep(0.02)
            source_pid, source_handle = json.loads(receipt.read_text())
            with duplicate_output(
                source_pid,
                windows_process_identity(source_pid),
                source_handle,
                resident_job=child.job,
            ) as stream:
                stream.write(b"broker output")
            assert output.read_bytes() == b"broker output"
            with (
                pytest.raises(OSError, match="identity changed"),
                duplicate_output(
                    source_pid,
                    "windows:0",
                    source_handle,
                    resident_job=child.job,
                ),
            ):
                pytest.fail("stale owner was accepted")
            with (
                pytest.raises(OSError, match="outside the resident Job"),
                duplicate_output(
                    os.getpid(),
                    windows_process_identity(os.getpid()),
                    msvcrt.get_osfhandle(sink.fileno()),
                    resident_job=child.job,
                ),
            ):
                pytest.fail("nonmember owner was accepted")
        finally:
            child.close()


def test_native_resident_service_stop_uses_cooperative_watcher(tmp_path):
    import msvcrt

    from bubo.update_windows_process import JobProcess
    from bubo.update_windows_resident import request_service_stop

    state = tmp_path / "state"
    state.mkdir()
    pid_path = state / "bubo-service.pid"
    finished = tmp_path / "graceful.txt"
    script = (
        "import threading\nfrom pathlib import Path\n"
        "from bubo import paths, service\n"
        "from bubo.signals import shutdown_requested\n"
        f"paths.SERVICE_PID = Path({str(pid_path)!r})\n"
        'service._write_pid("native-stop-token")\n'
        'service._watch_windows_stop("native-stop-token", threading.Event())\n'
        f"Path({str(finished)!r}).write_text(str(shutdown_requested()))\n"
    )
    with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as output:
        child = JobProcess(
            [sys.executable, "-c", script],
            name=new_job_name(),
            handles=tuple(
                msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, output, output)
            ),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        try:
            deadline = time.monotonic() + 10
            while not pid_path.exists():
                assert child.poll() is None
                assert time.monotonic() < deadline, "service watcher did not start"
                time.sleep(0.02)
            assert request_service_stop(state, child)
            assert child.wait(timeout=10) == 0
            assert child.quiescent()
            assert finished.read_text() == "True"
        finally:
            child.close()


def test_native_http_control_pipe_requests_exit_without_terminating_job(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bubo import update_windows_resident as resident

    ready = tmp_path / "ready"
    stopped = tmp_path / "stopped"
    script = (
        "from pathlib import Path\n"
        "from bubo.update_windows_resident import http_stop_event\n"
        "event = http_stop_event()\n"
        f'Path({str(ready)!r}).write_text("ready")\n'
        "assert event.wait(10)\n"
        f'Path({str(stopped)!r}).write_text("graceful")\n'
    )
    with open(os.devnull, "rb") as stdin, open(os.devnull, "wb") as output:
        monkeypatch.setattr(
            resident, "sys", SimpleNamespace(stdin=stdin, stdout=output, stderr=output)
        )
        child = resident.WindowsResidentProcess(
            [sys.executable, "-c", script],
            name=new_job_name(),
            cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            control_input=True,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert child.poll() is None
                assert time.monotonic() < deadline, "control reader did not start"
                time.sleep(0.02)
            assert child.request_stop()
            assert child.wait(timeout=5) == 0
            assert child.quiescent()
            assert stopped.read_text() == "graceful"
        finally:
            child.close()


def test_native_recovery_rejects_junction_without_touching_target(tmp_path):
    from bubo.update_installers import PythonInstaller, UnsafeInstallation

    adapter = PythonInstaller(tmp_path / "environment", tmp_path / "controller", executables={})
    adapter.environment.mkdir()
    (adapter.environment / "live").write_text("live data")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "snapshot.json").write_text(json.dumps({"environment": str(adapter.environment)}))
    external = tmp_path / "external"
    external.mkdir()
    (external / "valuable").write_text("external data")
    junction = snapshot / "environment"
    subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(UnsafeInstallation, match="reparse point"):
            adapter.restore(snapshot)
        assert (adapter.environment / "live").read_text() == "live data"
        assert (external / "valuable").read_text() == "external data"
        assert junction.is_junction()
    finally:
        junction.rmdir()


def test_native_resident_readiness_accepts_only_live_job_member(tmp_path):
    import uuid

    from bubo.update_identity import atomic_json
    from bubo.update_readiness import ResidentReadiness, StartupRejected
    from bubo.update_windows_resident import WindowsResidentProcess
    from bubo.version import installed_version

    state, installation = tmp_path / "state", tmp_path / "controller"
    state.mkdir()
    installation.mkdir()
    atomic_json(state / "update-registration.json", {"installation": str(installation)})
    attempt = {
        "attempt_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "states": [str(state)],
        "phase": "migration",
        "target": installed_version(),
        "source": "0.0.1",
    }
    atomic_json(installation / "attempt.json", attempt)
    atomic_json(installation / "maintenance.json", attempt)
    receipt = ResidentReadiness(state, "service", outcome="committed")
    script = (
        "from pathlib import Path\nimport time\n"
        "from bubo.update_readiness import ResidentStartup\n"
        f'gate = ResidentStartup.from_environment("service", Path({str(state)!r}))\n'
        "gate.signal_ready()\ntime.sleep(60)\n"
    )
    child = WindowsResidentProcess(
        [sys.executable, "-c", script],
        name=new_job_name(),
        cwd=str(tmp_path),
        env=receipt.environment(dict(os.environ)),
        probe=True,
    )
    try:
        receipt.bind(child)
        receipt.wait()
        assert receipt.verify()
        ready = json.loads(receipt.ready_path.read_text())
        assert child.owns_member(ready["pid"], ready["process_identity"])
        # A valid live process identity outside this Job must still be rejected.
        atomic_json(
            receipt.ready_path,
            {
                **ready,
                "pid": os.getpid(),
                "process_identity": windows_process_identity(os.getpid()),
            },
        )
        with pytest.raises(StartupRejected):
            receipt.verify()
        atomic_json(receipt.ready_path, ready)
        child.close()
        with pytest.raises(StartupRejected):
            receipt.verify()
    finally:
        child.close()


def test_native_startup_claim_cannot_replace_existing_claim(tmp_path):
    from bubo.update_windows_files import claim_file

    claim = tmp_path / "startup.claim"
    claim_file(claim)
    before = claim.read_bytes()
    with pytest.raises(FileExistsError):
        claim_file(claim)
    assert claim.read_bytes() == before
    assert list(tmp_path.iterdir()) == [claim]


@pytest.mark.skipif(
    os.environ.get("BUBO_NATIVE_SCHEDULER_TEST") != "1",
    reason="requires opt-in for a temporary user scheduled task",
)
def test_native_user_scheduler_runs_and_removes_owned_temporary_helper(tmp_path):
    import hashlib
    import zipfile

    from bubo.update_windows_schedule import schedule

    installation = tmp_path / "scheduler controller Ω"
    installation.mkdir()
    marker = installation / "ran.txt"
    archive = installation / "helper-native.pyz"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            "__main__.py", f'from pathlib import Path\nPath({str(marker)!r}).write_text("ran")\n'
        )
    record = {"python": sys._base_executable, "archive": str(archive)}

    def runner(*args, **kwargs):
        try:
            return subprocess.run(*args, **kwargs)
        except subprocess.CalledProcessError as error:
            pytest.fail(f"Native task scheduler: {error.stderr.decode(errors='replace')}")

    task = "Bubo-Update-" + hashlib.sha256(str(installation).encode()).hexdigest()[:24]
    query = ["schtasks", "/Query", "/TN", task, "/XML"]
    assert subprocess.run(query, capture_output=True).returncode != 0
    try:
        assert schedule(installation, record, runner=runner) == task
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.read_text() == "ran"
        assert schedule(installation, record, runner=runner) == task
    finally:
        if subprocess.run(query, capture_output=True).returncode == 0:
            schedule(installation, record, remove=True, runner=runner)
    assert subprocess.run(query, capture_output=True).returncode != 0


def test_native_directory_flush_and_matched_database_restore(tmp_path):
    import sqlite3
    from contextlib import closing

    from bubo.update_identity import sync_directory
    from bubo.updater import _backup_db, _restore_db

    database, backup = tmp_path / "state.sqlite", tmp_path / "backup.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE value (number INTEGER)")
        connection.execute("INSERT INTO value VALUES (1)")
    assert _backup_db(database, backup)
    sync_directory(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE value SET number = 2")
    _restore_db(backup, database)
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("SELECT number FROM value").fetchone() == (1,)
    _restore_db(None, database)
    assert not database.exists()
    assert backup.exists()


def test_native_environment_exclusive_lease_allows_config_copy_and_directory_restore(
    tmp_path, monkeypatch
):
    import shutil

    from bubo import update_helper
    from bubo.update_admission import (
        MaintenanceInProgress,
        UpdateBusy,
        activity,
        environment_lock,
        exclusive,
    )
    from bubo.update_identity import atomic_json
    from bubo.updater import UpdateEngine

    environment = tmp_path / "environment"
    environment.mkdir()
    configuration = environment / "pyvenv.cfg"
    configuration.write_text("home = synthetic fixture\n")
    original = configuration.read_bytes()
    state, installation = tmp_path / "state", tmp_path / "controller"
    state.mkdir()
    installation.mkdir()
    (state / "update-activity.lock").touch()
    for name in ("activity.lock", "registry.lock"):
        (installation / name).touch()
    atomic_json(state / "update-registration.json", {"installation": str(installation)})
    atomic_json(installation / "installation.json", {"environment": str(environment)})
    monkeypatch.setattr(sys, "prefix", str(environment))
    monkeypatch.setattr(update_helper, "installation_for_environment", lambda path: installation)
    engine = object.__new__(UpdateEngine)
    engine.installation = installation
    engine.fence = installation / "maintenance.json"
    with environment_lock(environment, exclusive=True) as bridge:
        assert configuration.read_bytes() == original
        shutil.copytree(environment, tmp_path / "snapshot")
        with pytest.raises(UpdateBusy), environment_lock(environment):
            pytest.fail("Exclusive environment bridge was bypassed")
        with exclusive(installation, [state]):
            atomic_json(engine.fence, {"token": "synthetic-fence"})
            engine._release_bridge(bridge)
            assert bridge.closed
            os.replace(environment, tmp_path / "failed")
            shutil.copytree(tmp_path / "snapshot", environment)
            assert configuration.read_bytes() == original
            with pytest.raises(MaintenanceInProgress), activity(state):
                pytest.fail("Replacement config inode bypassed installation admission")
    assert (tmp_path / "failed" / "pyvenv.cfg").read_bytes() == original
    with pytest.raises(MaintenanceInProgress), activity(state):
        pytest.fail("Replacement config inode bypassed durable maintenance intent")


@pytest.mark.parametrize("failure", [None, "install", "migration", "crash", "locked_restore"])
def test_native_python_transaction_preserves_code_database_and_identity(
    tmp_path, monkeypatch, request, failure
):
    import hashlib
    import sqlite3
    import uuid
    import venv
    import zipfile
    from contextlib import closing

    from test_update_installers import Download, wheel

    from bubo.analytics_config import AnalyticsConfig
    from bubo.update_helper import installation_for_environment
    from bubo.update_identity import atomic_json
    from bubo.update_installers import PythonInstaller
    from bubo.updater import Release, UpdateEngine

    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "Scripts" / "python.exe"

    def package(version):
        artifact = wheel(tmp_path, version)
        with zipfile.ZipFile(artifact) as archive:
            contents = {name: archive.read(name) for name in archive.namelist()}
        contents["bubo/paths.py"] = (
            b"import os\nfrom pathlib import Path\nDB = Path(os.environ['BUBO_UPDATE_EXPECTED_DB'])\n"
        )
        contents["bubo/db.py"] = (
            "import sqlite3\nfrom contextlib import closing\nfrom .paths import DB\n"
            "def init_db():\n"
            "    with closing(sqlite3.connect(DB)) as connection, connection:\n"
            + (
                "        connection.execute('CREATE TABLE IF NOT EXISTS migrated (id INTEGER)')\n"
                if version == "0.27.0"
                else "        pass\n"
            )
        ).encode()
        record = f"bubo-{version}.dist-info/RECORD"
        contents[record] = "".join(f"{name},,\n" for name in contents).encode()
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
        return artifact

    subprocess.run(
        [str(python), "-m", "pip", "install", str(package("0.26.0"))],
        check=True,
        capture_output=True,
        timeout=60,
    )
    content = package("0.27.0").read_bytes()
    installation = installation_for_environment(environment)
    installation.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    for name in ("attempt.lock", "registry.lock", "activity.lock"):
        (installation / name).touch()
    (state / "update-activity.lock").touch()
    atomic_json(
        installation / "installation.json",
        {
            "environment": str(environment),
            "states": [str(state)],
        },
    )
    atomic_json(state / "update-registration.json", {"installation": str(installation)})
    identity = str(uuid.uuid4())
    (state / "install_id").write_text(identity)
    with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection, connection:
        connection.execute("CREATE TABLE reviews (body TEXT)")
        connection.execute("INSERT INTO reviews VALUES ('original data')")
    adapter = PythonInstaller(environment, installation, opener=lambda *a, **k: Download(content))
    locked_handles = []

    def release_lock():
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        while locked_handles:
            assert api.CloseHandle(locked_handles.pop())

    request.addfinalizer(release_lock)
    if failure in {"install", "migration", "locked_restore"}:
        name = "activate" if failure == "install" else "migrate_and_check"
        original = getattr(adapter, name)

        def fail_after_mutation(*args):
            original(*args)
            if failure == "locked_restore":
                import ctypes
                from ctypes import wintypes

                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.CreateFileW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ]
                api.CreateFileW.restype = wintypes.HANDLE
                handle = api.CreateFileW(
                    str(environment / "Scripts" / "bubo.exe"),
                    0x80000000,
                    3,
                    None,
                    3,
                    0x80,
                    None,
                )
                assert handle not in (None, ctypes.c_void_p(-1).value)
                locked_handles.append(handle)
            raise OSError("synthetic post-mutation failure")

        # Recovery must validate the restored runtime normally.
        def fail_once(*args):
            setattr(adapter, name, original)
            fail_after_mutation(*args)

        monkeypatch.setattr(adapter, name, fail_once)
    engine = UpdateEngine(
        installation,
        [state],
        adapter,
        source_version="0.26.0",
        producer_version="0.26.0",
        analytics=AnalyticsConfig(enabled=False),
        enabled=lambda _: True,
    )
    release = Release(
        "0.27.0",
        "https://files.pythonhosted.org/packages/bubo.whl",
        hashlib.sha256(content).hexdigest(),
    )
    if failure == "crash":
        script = tmp_path / "crash_update.py"
        script.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            f"Path.home = classmethod(lambda cls: Path({str(Path.home())!r}))\n"
            f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
            "from test_update_installers import Download\n"
            "from bubo.analytics_config import AnalyticsConfig\n"
            "from bubo.update_installers import PythonInstaller\n"
            "from bubo.updater import Release, UpdateEngine\n"
            f"environment = Path({str(environment)!r})\n"
            f"installation = Path({str(installation)!r})\n"
            f"state = Path({str(state)!r})\n"
            f"content = Path({str(tmp_path / 'bubo-0.27.0-py3-none-any.whl')!r}).read_bytes()\n"
            "adapter = PythonInstaller(environment, installation, opener=lambda *a, **k: Download(content))\n"
            "migrate = adapter.migrate_and_check\n"
            "def crash(*args):\n"
            "    migrate(*args)\n"
            "    os._exit(9)\n"
            "adapter.migrate_and_check = crash\n"
            'engine = UpdateEngine(installation, [state], adapter, source_version="0.26.0", '
            'producer_version="0.26.0", analytics=AnalyticsConfig(enabled=False), enabled=lambda _: True)\n'
            f"print(engine.run(Release({release.version!r}, {release.artifact!r}, {release.sha256!r})))\n"
        )
        process = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=90)
        assert process.returncode == 9, (process.stdout, process.stderr)
        assert engine.fence.exists()
        result = engine.recover()
    else:
        result = engine.run(release)
    if failure == "locked_restore":
        from bubo.update_reporting import FailureStore

        assert result.code == "recovery_blocked", result
        assert engine.fence.exists()
        assert FailureStore(state).status()[-1]["recovery_status"] == "blocked"
        release_lock()
        result = engine.recover()
    assert result.status == ("failed" if failure else "updated"), result
    if failure:
        assert result.code == "restored", result
    assert subprocess.check_output(
        [str(environment / "Scripts" / "bubo.exe")], text=True
    ).strip() == ("0.26.0" if failure else "0.27.0")
    with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection:
        assert connection.execute("SELECT body FROM reviews").fetchall() == [("original data",)]
        migrated = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'migrated'"
        ).fetchone()
        assert bool(migrated) is (failure is None)
    assert (state / "install_id").read_text() == identity
    assert not engine.fence.exists()
    assert adapter.writers_quiescent()


def test_native_retained_launcher_serves_real_mcp_and_reconnects(tmp_path):
    import asyncio
    import shutil

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from bubo import update_helper
    from bubo.update_identity import atomic_json
    from bubo.update_platform import current_owner

    environment = Path(sys.prefix)
    # The retained archive and real MCP child run outside pytest's Path.home patch.
    # Register only this isolated test venv in their shared real registry, then remove it.
    installation = update_helper.installation_for_environment(
        environment,
        home=Path(os.path.expanduser("~")),
    )
    assert not installation.exists(), "Native test requires an unregistered isolated test venv"
    root = tmp_path / "root"
    state = root / "var" / "state"
    state.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "env.toml").write_text("auto_update = false\n[analytics]\nenabled = false\n")
    installation.mkdir(parents=True)
    try:
        for name in ("activity.lock", "registry.lock"):
            (installation / name).touch()
        (state / "update-activity.lock").touch()
        archive = update_helper._bundle(installation, Path(sys._base_executable))
        atomic_json(
            installation / "installation.json",
            {
                "protocol_version": 1,
                "owner": current_owner(),
                "ready": True,
                "environment": str(environment),
                "python": sys._base_executable,
                "archive": archive,
                "states": [str(state)],
            },
        )
        atomic_json(
            state / "update-registration.json",
            {
                "installation": str(installation),
                "root": str(root),
                "ready": True,
            },
        )
        launch = update_helper.resident_launch(installation, state, "mcp-stdio")
        parameters = StdioServerParameters(
            command=launch["command"],
            args=launch["args"],
            env={**os.environ, **launch["env"]},
        )

        async def connect():
            async with (
                asyncio.timeout(30),
                stdio_client(parameters) as streams,
                ClientSession(*streams) as session,
            ):
                result = await session.initialize()
                assert result.serverInfo.name
                result = await session.list_tools()
                assert result.tools
                result = await session.call_tool("health")
                assert not result.isError, result

        asyncio.run(connect())
        asyncio.run(connect())
        owners = list((installation / "supervisors").glob("*/owner.json"))
        assert len(owners) == 2
        assert all(json.loads(owner.read_text())["closed"] for owner in owners)
    finally:
        shutil.rmtree(installation)


@pytest.mark.parametrize("registered", [False, True])
def test_native_legacy_resident_excludes_updates_until_process_exit(tmp_path, registered):
    from bubo.update_admission import UpdateBusy, _file_lock, environment_lock
    from bubo.update_helper import installation_for_environment
    from bubo.update_identity import atomic_json

    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "pyvenv.cfg").write_text("home = fixture\n")
    installation = installation_for_environment(environment)
    if registered:
        installation.mkdir(parents=True)
        (installation / "activity.lock").touch()
        atomic_json(installation / "installation.json", {"environment": str(environment)})
    marker = tmp_path / "started"
    code = (
        "import sys, time\nfrom pathlib import Path\n"
        f"Path.home = classmethod(lambda cls: Path({str(Path.home())!r}))\n"
        f"sys.prefix = {str(environment)!r}\n"
        "from bubo.update_supervisor import delegate\n"
        'delegate("mcp-stdio")\n'
        f"Path({str(marker)!r}).touch()\n"
        "time.sleep(120)\n"
    )
    env = dict(os.environ)
    env.pop("BUBO_RESIDENT_CHILD", None)
    env.pop("BUBO_UPDATE_START_REQUEST", None)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    process = subprocess.Popen([sys._base_executable, "-c", code], env=env, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert process.poll() is None, process.stderr.read().decode(errors="replace")
            assert time.monotonic() < deadline
            time.sleep(0.02)

        def admission():
            return (
                _file_lock(installation / "activity.lock", exclusive=True)
                if registered
                else environment_lock(environment, exclusive=True)
            )

        with pytest.raises(UpdateBusy), admission():
            pytest.fail("A live legacy resident allowed replacement")
        process.terminate()
        process.wait(timeout=10)
        with admission():
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        process.stderr.close()


def test_native_legacy_snapshot_exempts_only_own_redirector(tmp_path):
    from bubo.update_windows_legacy import check_legacy_processes

    environment = Path(sys.prefix)
    check_legacy_processes(environment)
    marker = tmp_path / "legacy-started"
    code = f"import time; from pathlib import Path; Path({str(marker)!r}).touch(); time.sleep(120)"
    process = subprocess.Popen([sys._base_executable, "-c", code, str(environment / "legacy.py")])
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        with pytest.raises(OSError, match="Close legacy Bubo"):
            check_legacy_processes(environment)
    finally:
        process.terminate()
        process.wait(timeout=10)
    check_legacy_processes(environment)


@pytest.mark.skipif(
    os.environ.get("BUBO_NATIVE_SCHEDULER_TEST") != "1", reason="Native scheduler opt-in required"
)
def test_native_windows_bootstrap_repeat_and_uninstall(tmp_path, monkeypatch):
    import hashlib
    import venv

    from test_update_installers import wheel

    from bubo import update_helper
    from bubo.update_windows_schedule import _query, schedule

    monkeypatch.setenv("PIP_NO_INDEX", "1")
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run(
        [
            str(environment / "Scripts" / "python.exe"),
            "-m",
            "pip",
            "install",
            str(wheel(tmp_path, "0.27.0")),
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "env.toml").write_text("auto_update = false\n[analytics]\nenabled = false\n")
    installation = update_helper.installation_for_environment(environment)
    task = "Bubo-Update-" + hashlib.sha256(str(installation).encode()).hexdigest()[:24]

    def native_schedule(installation, record, **kwargs):
        return schedule(installation, record)

    try:
        assert (
            update_helper.bootstrap(root, environment=environment, schedule=native_schedule)
            == installation
        )
        registration = json.loads((installation / "installation.json").read_text())
        state = root / "var" / "state"
        assert json.loads((state / "update-registration.json").read_text())["ready"]
        assert registration["states"] == [str(state)]
        assert _query(task, subprocess.run) is not None
        assert (
            update_helper.bootstrap(root, environment=environment, schedule=native_schedule)
            == installation
        )
        assert json.loads((installation / "installation.json").read_text())["states"] == [
            str(state)
        ]
        update_helper.uninstall_helper(installation)
        assert _query(task, subprocess.run) is None
        assert (installation / "disabled.json").exists()
    finally:
        if _query(task, subprocess.run) is not None:
            update_helper.uninstall_helper(installation)


def _production_fixture_wheel(directory, version):
    import zipfile
    from importlib.metadata import distribution

    source = Path(__file__).parents[1] / "src" / "bubo"
    contents = {
        "bubo/" + path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    contents["bubo/__init__.py"] = f"__version__ = {version!r}\n".encode()
    metadata = f"Metadata-Version: 2.1\nName: bubo\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {item}\n" for item in distribution("bubo").requires)
    info = f"bubo-{version}.dist-info/"
    contents[info + "METADATA"] = metadata.encode()
    contents[info + "WHEEL"] = (
        b"Wheel-Version: 1.0\nGenerator: native-acceptance\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    contents[info + "entry_points.txt"] = (
        b"[console_scripts]\nbubo = bubo.poller:main\nbubo-poller = bubo.poller:main\n"
        b"bubo-mcp = bubo.mcp_server:main\n"
    )
    contents[info + "RECORD"] = "".join(
        f"{name},,\n" for name in [*contents, info + "RECORD"]
    ).encode()
    artifact = directory / f"bubo-{version}-py3-none-any.whl"
    with zipfile.ZipFile(artifact, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    return artifact


@pytest.mark.parametrize("fail_migration", [False, True])
def test_native_real_mcp_package_replacement_and_matched_rollback(
    tmp_path, monkeypatch, fail_migration
):
    import hashlib
    import shutil
    import sqlite3
    import uuid
    import venv
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import closing

    from test_update_installers import Download

    from bubo import update_helper
    from bubo.analytics_config import AnalyticsConfig
    from bubo.update_installers import PythonInstaller
    from bubo.update_supervisor import ResidentController
    from bubo.updater import Release, UpdateEngine

    installed_uv = sorted((Path(sys.prefix).parent / "tools").rglob("uv.exe"))
    uv = shutil.which("uv") or (str(installed_uv[0]) if installed_uv else None)
    if uv is None:
        pytest.skip("Requires uv for a disposable full dependency installation")
    real_home = Path(os.path.expanduser("~"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: real_home))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "Scripts" / "python.exe"
    old = _production_fixture_wheel(tmp_path, "0.27.0")
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(old)],
        capture_output=True,
        check=True,
        timeout=180,
    )
    new = _production_fixture_wheel(tmp_path, "0.28.0").read_bytes()
    root = tmp_path / "root"
    (root / "config").mkdir(parents=True)
    (root / "config" / "env.toml").write_text("auto_update = false\n[analytics]\nenabled = false\n")
    state = root / "var" / "state"
    installation = update_helper.installation_for_environment(environment)
    assert not installation.exists()
    processes = []
    readers = ThreadPoolExecutor(max_workers=1)
    try:
        update_helper.bootstrap(root, environment=environment, schedule=lambda *a, **k: None)
        adapter = PythonInstaller(
            environment, installation, opener=lambda *a, **k: Download(new), timeout=180
        )
        adapter.migrate_and_check([state], "initial-fixture")
        identity = str(uuid.uuid4())
        (state / "install_id").write_text(identity)
        with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection, connection:
            connection.execute("CREATE TABLE native_preserved (value TEXT)")
            connection.execute("INSERT INTO native_preserved VALUES ('fixture data')")
        launch = update_helper.resident_launch(installation, state, "mcp-stdio")

        def connect():
            process = subprocess.Popen(
                [launch["command"], *launch["args"]],
                env={**os.environ, **launch["env"]},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            processes.append(process)

            def send(value):
                process.stdin.write((json.dumps(value) + "\n").encode())
                process.stdin.flush()

            def response():
                line = readers.submit(process.stdout.readline).result(timeout=30)
                assert line, f"MCP exited with {process.poll()}"
                result = json.loads(line)
                assert "error" not in result, result
                return result

            send(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "native-acceptance", "version": "1"},
                    },
                }
            )
            assert response()["id"] == 1
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "health", "arguments": {}},
                }
            )
            result = response()
            assert result["id"] == 2, result
            assert not result["result"].get("isError"), result
            return process

        resident = connect()
        migrate = adapter.migrate_and_check

        def target_migration(*args):
            adapter.migrate_and_check = migrate
            migrate(*args)
            with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection, connection:
                connection.execute("CREATE TABLE native_target_migration (id INTEGER)")
            if fail_migration:
                raise OSError("Synthetic failure after real migration")

        monkeypatch.setattr(adapter, "migrate_and_check", target_migration)
        engine = UpdateEngine(
            installation,
            [state],
            adapter,
            source_version="0.27.0",
            producer_version="0.27.0",
            residents=ResidentController(installation, timeout=60),
            analytics=AnalyticsConfig(enabled=False),
            enabled=lambda _: True,
        )
        recovery_errors = []
        record_failure = engine._record

        def traced_failure(*args, **kwargs):
            import traceback

            recovery_errors.append(traceback.format_exc())
            return record_failure(*args, **kwargs)

        monkeypatch.setattr(engine, "_record", traced_failure)
        result = engine.run(
            Release(
                "0.28.0",
                "https://files.pythonhosted.org/packages/bubo.whl",
                hashlib.sha256(new).hexdigest(),
            )
        )
        assert result.status == ("failed" if fail_migration else "updated"), result
        if fail_migration:
            assert result.code == "restored", (result, recovery_errors)
        assert (
            resident.wait(timeout=20) == 75
        )  # Existing client must reconnect after idle replacement.
        assert resident.stdout.read() == b""
        replacement = connect()
        replacement.stdin.close()
        assert replacement.wait(timeout=20) == 0
        expected = "0.27.0" if fail_migration else "0.28.0"
        actual = subprocess.check_output(
            [
                str(python),
                "-c",
                "from bubo.version import installed_version; print(installed_version())",
            ],
            text=True,
        )
        assert actual.strip() == expected
        with closing(sqlite3.connect(state / "reviewer.sqlite")) as connection:
            assert connection.execute("SELECT value FROM native_preserved").fetchall() == [
                ("fixture data",)
            ]
            assert (
                bool(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='native_target_migration'"
                    ).fetchone()
                )
                is not fail_migration
            )
        assert (state / "install_id").read_text() == identity
        assert not engine.fence.exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
        readers.shutdown(wait=True, cancel_futures=True)
        if installation.exists():
            if (installation / "maintenance.json").exists():
                shutil.copytree(installation, tmp_path / "controller-debug")
            shutil.rmtree(installation)
