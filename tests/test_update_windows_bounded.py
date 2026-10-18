import inspect
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from bubo import update_windows_bounded as bounded


@pytest.fixture
def guardian(monkeypatch):
    """Run the actual stdlib guardian, substituting only the Windows native job API."""
    children = []
    native_signature = inspect.signature(bounded.JobProcess)

    class Process:
        def __init__(self, args, *, env, cwd, handles, **kwargs):
            native_signature.bind(args, env=env, cwd=cwd, handles=handles, **kwargs)
            self.name = kwargs["name"]
            if kwargs.get("on_job_created"):
                kwargs["on_job_created"]()
            self.proc = subprocess.Popen(
                args,
                env=env,
                cwd=cwd,
                stdin=handles[0],
                stdout=handles[1],
                stderr=handles[2],
                start_new_session=True,
            )
            self.api = self
            self.job = self.proc.pid
            self.closed = False
            self.receipt = Path(env[args[5]])
            children.append(self)

        def wait(self, timeout):
            return self.proc.wait(timeout=timeout)

        def terminate(self):
            if self.proc.poll() is None:
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)

        def close(self):
            self.terminate()
            self.closed = True

    monkeypatch.setattr(bounded, "JobProcess", Process)
    monkeypatch.setattr(bounded, "_wait_empty", lambda *args: None)
    # Imported inside the target function; do not interfere with subprocess module initialization.
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    return children


@pytest.mark.parametrize("status", [0, 7])
def test_guardian_preserves_combined_transcript_status_and_cwd(tmp_path, guardian, status):
    script = (
        'import os, sys; os.write(1, b"out\\r\\n"); os.write(2, b"err\\r\\n"); '
        "print(os.getcwd(), flush=True); print(sys.stdin.read(), flush=True); "
        f"raise SystemExit({status})"
    )
    args = [sys.executable, "-c", script]
    result = bounded.run_bounded_windows(args, cwd=tmp_path, timeout=5)
    assert result.args == args
    assert result.returncode == status
    assert result.stdout == f"out\nerr\n{tmp_path}\n\n"
    assert result.stderr is None
    assert guardian[0].closed
    assert not guardian[0].receipt.exists()


def test_guardian_preserves_original_environment_and_keeps_control_out_of_child(guardian):
    env = {
        **os.environ,
        "TEST_SECRET": "synthetic-memory-only",
        "_BUBO_BOUNDED_EXISTING": "kept",
        "PYTHONPATH": "/deliberately/not/a/module/path",
    }
    script = (
        "import json, os; print(json.dumps({k: v for k, v in os.environ.items() "
        'if k.startswith("_BUBO_BOUNDED_") or k in {"TEST_SECRET", "PYTHONPATH"}}))'
    )
    result = bounded.run_bounded_windows([sys.executable, "-c", script], env=env, timeout=5)
    assert json.loads(result.stdout) == {
        "TEST_SECRET": "synthetic-memory-only",
        "_BUBO_BOUNDED_EXISTING": "kept",
        "PYTHONPATH": "/deliberately/not/a/module/path",
    }
    assert not guardian[0].receipt.exists()


def test_guardian_reports_missing_executable_as_launch_error(guardian):
    with pytest.raises(FileNotFoundError, match="not found"):
        bounded.run_bounded_windows(["/nonexistent-bubo-command"], timeout=5)
    assert guardian[0].closed


def test_guardian_timeout_preserves_partial_combined_output(guardian):
    script = 'import os, time; os.write(1, b"started\\r\\n"); time.sleep(30)'
    args = [sys.executable, "-c", script]
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        bounded.run_bounded_windows(args, timeout=1)
    assert failure.value.cmd == args
    assert failure.value.output == "started\n"
    assert failure.value.timeout == 1
    assert guardian[0].closed


def test_descendant_quiescence_timeout_is_not_reported_as_success(guardian, monkeypatch):
    def surviving(*args):
        raise TimeoutError("descendants")

    monkeypatch.setattr(bounded, "_wait_empty", surviving)
    args = [sys.executable, "-c", 'print("finished root")']
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        bounded.run_bounded_windows(args, timeout=5)
    assert failure.value.output == "finished root\n"
    assert guardian[0].closed


def test_guardian_tracks_and_retires_registered_job(tmp_path, guardian, monkeypatch):
    from bubo import update_windows_jobs as jobs

    monkeypatch.setattr(jobs, "current_owner", lambda: "S-test-owner")
    monkeypatch.setattr(jobs, "windows_process_identity", lambda _pid: "owner-start")
    events = []
    original = jobs.atomic_json

    def publish(path, record):
        events.append(dict(record))
        original(path, record)

    monkeypatch.setattr(jobs, "atomic_json", publish)
    result = bounded.run_bounded_windows(
        [sys.executable, "-c", 'print("memory-secret")'],
        installation=tmp_path,
        env={**os.environ, "TOKEN": "memory-secret"},
        timeout=5,
    )
    assert result.stdout == "memory-secret\n"
    assert [record["job_created"] for record in events] == [False, True, True]
    assert events[-1]["state"] == "quiescent"
    assert all("memory-secret" not in json.dumps(record) for record in events)
    assert guardian[0].closed
    assert list((tmp_path / "windows-jobs").iterdir()) == []


def test_failed_guardian_close_is_recoverable_while_caller_survives(
    tmp_path, guardian, monkeypatch
):
    from bubo import update_windows_jobs as jobs

    monkeypatch.setattr(jobs, "current_owner", lambda: "S-test-owner")
    monkeypatch.setattr(jobs, "windows_process_identity", lambda _pid: "owner-start")
    recovered = []
    monkeypatch.setattr(jobs, "recover_job", recovered.append)

    def failed_close(_self):
        raise TimeoutError("whole-job shutdown unproven")

    monkeypatch.setattr(bounded.JobProcess, "close", failed_close)
    with pytest.raises(TimeoutError, match="shutdown unproven"):
        bounded.run_bounded_windows(
            [sys.executable, "-c", 'print("done")'],
            installation=tmp_path,
            timeout=5,
        )
    receipts = list((tmp_path / "windows-jobs").glob("*.json"))
    assert len(receipts) == 1
    record = json.loads(receipts[0].read_text())
    assert record["state"] == "abandoned"
    assert record["job_created"] is True
    jobs.drain(tmp_path)
    assert recovered == [guardian[0].name]
    assert not receipts[0].exists()


@pytest.fixture
def public_wrapper(tmp_path, guardian, monkeypatch):
    from bubo import (
        paths,
        subproc,
        update_admission,
        update_locking,
        update_windows_files,
        update_windows_jobs,
    )

    def environment_lease(configuration, *, exclusive):
        stream = configuration.open("rb")
        try:
            update_locking.lock_file(stream.fileno(), exclusive=exclusive)
        except BaseException:
            stream.close()
            raise
        return stream

    # This fixture runs real POSIX processes while simulating Windows dispatch.
    monkeypatch.setattr(update_locking, "open_windows_environment_lease", environment_lease)

    state, installation = tmp_path / "state", tmp_path / "installation"
    state.mkdir()
    installation.mkdir()
    (installation / "activity.lock").touch()
    (state / "update-activity.lock").touch()
    events = []
    monkeypatch.setattr(subproc.sys, "platform", "win32")
    monkeypatch.setattr(update_windows_files, "replace_file", os.replace)
    monkeypatch.setattr(paths, "DB", state / "bubo.db")
    (state / "update-registration.json").write_text(json.dumps({"installation": str(installation)}))
    (installation / "generation.json").write_text(
        json.dumps({"version": update_admission._PROCESS_VERSION})
    )
    monkeypatch.setattr(update_windows_jobs, "current_owner", lambda: "test-owner")
    monkeypatch.setattr(update_windows_jobs, "windows_process_identity", lambda pid: "parent-start")

    actual_activity = update_admission.activity

    @contextmanager
    def admission(actual, *, writable):
        assert actual == state
        assert writable is False
        with actual_activity(actual, writable=writable):
            locked_installation = Path(
                json.loads((actual / "update-registration.json").read_text())["installation"]
            )
            events.append("enter")
            try:
                yield
            finally:
                with pytest.raises(update_admission.UpdateBusy):
                    with update_admission._file_lock(
                        locked_installation / "activity.lock", exclusive=True
                    ):
                        pytest.fail("Admission released before bounded cleanup")
                events.append(("exit", [child.closed for child in guardian]))

    monkeypatch.setattr(update_admission, "activity", admission)
    return SimpleNamespace(run=subproc.run_bounded, events=events, installation=installation)


def test_public_wrapper_preserves_combined_output_status_environment_and_cwd(
    public_wrapper, tmp_path
):
    args = [
        sys.executable,
        "-c",
        (
            "import os, sys; print(os.environ['BOUNDARY_VALUE']); print(os.getcwd()); "
            "sys.stdout.flush(); print('stderr line', file=sys.stderr); raise SystemExit(7)"
        ),
    ]
    result = public_wrapper.run(
        args, cwd=tmp_path, env={**os.environ, "BOUNDARY_VALUE": "value"}, timeout=5
    )
    assert result.args == args
    assert result.returncode == 7
    assert result.stdout == f"value\n{tmp_path}\nstderr line\n"
    assert result.stderr is None
    assert public_wrapper.events == ["enter", ("exit", [True])]
    assert list((public_wrapper.installation / "windows-jobs").iterdir()) == []


def test_public_wrapper_preserves_partial_timeout_output_and_cleanup(public_wrapper):
    args = [sys.executable, "-c", "import time; print('partial', flush=True); time.sleep(30)"]
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        public_wrapper.run(args, timeout=1)
    assert failure.value.cmd == args
    assert failure.value.timeout == 1
    assert failure.value.output == "partial\n"
    assert public_wrapper.events == ["enter", ("exit", [True])]
    assert list((public_wrapper.installation / "windows-jobs").iterdir()) == []


def test_public_wrapper_preserves_actionable_missing_command_diagnostic(public_wrapper):
    with pytest.raises(FileNotFoundError) as failure:
        public_wrapper.run(["/not/a/bubo-command"], timeout=5)
    assert "could not launch the command" in str(failure.value)
    assert "executable was not found on PATH" in str(failure.value)
    assert "[agents].reviewer_command" in str(failure.value)
    assert isinstance(failure.value.__cause__, FileNotFoundError)
    assert public_wrapper.events == ["enter", ("exit", [True])]


def test_admission_failure_is_not_mislabeled_as_missing_command(public_wrapper, monkeypatch):
    from bubo import update_admission

    @contextmanager
    def missing_admission(*args, **kwargs):
        raise FileNotFoundError("installation registration disappeared")
        yield  # pragma: no cover

    monkeypatch.setattr(update_admission, "activity", missing_admission)
    with pytest.raises(FileNotFoundError, match="registration disappeared") as failure:
        public_wrapper.run(["valid-command"], timeout=5)
    assert "reviewer_command" not in str(failure.value)
    assert not (public_wrapper.installation / "windows-jobs").exists()


def test_receipt_storage_failure_is_not_mislabeled_as_missing_command(public_wrapper, monkeypatch):
    def missing_receipt(*args, **kwargs):
        raise FileNotFoundError("Windows ownership receipt disappeared")

    monkeypatch.setattr(bounded, "run_bounded_windows", missing_receipt)
    with pytest.raises(FileNotFoundError, match="ownership receipt disappeared") as failure:
        public_wrapper.run(["valid-command"], timeout=5)
    assert "reviewer_command" not in str(failure.value)


def test_failed_bounded_cleanup_retains_abandonment_before_admission_release(
    public_wrapper, monkeypatch
):
    def failed_close(self):
        raise TimeoutError("descendant shutdown unproven")

    monkeypatch.setattr(bounded.JobProcess, "close", failed_close)
    with pytest.raises(TimeoutError, match="descendant shutdown unproven"):
        public_wrapper.run([sys.executable, "-c", 'print("done")'], timeout=5)
    receipts = list((public_wrapper.installation / "windows-jobs").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["state"] == "abandoned"
    assert public_wrapper.events == ["enter", ("exit", [False])]


def test_registration_change_before_admission_never_selects_stale_job_ledger(
    public_wrapper, tmp_path, monkeypatch
):
    from bubo import paths, update_admission

    other = tmp_path / "other-installation"
    other.mkdir()
    (other / "activity.lock").touch()
    (other / "generation.json").write_text(
        json.dumps({"version": update_admission._PROCESS_VERSION})
    )
    original = update_admission.activity

    @contextmanager
    def changed(state, *, writable):
        (state / "update-registration.json").write_text(json.dumps({"installation": str(other)}))
        with original(state, writable=writable):
            yield

    monkeypatch.setattr(update_admission, "activity", changed)
    with pytest.raises(update_admission.MaintenanceInProgress, match="registration changed"):
        public_wrapper.run([sys.executable, "-c", 'print("must not run")'], timeout=5)
    assert not (other / "windows-jobs").exists()
    assert not (public_wrapper.installation / "windows-jobs").exists()
    assert json.loads((paths.DB.parent / "update-registration.json").read_text())[
        "installation"
    ] == str(other)


def test_registration_change_during_lock_acquisition_blocks_admission(
    public_wrapper, tmp_path, monkeypatch
):
    from bubo import update_admission

    original = update_admission._file_lock

    @contextmanager
    def changed(path, *, exclusive, **kwargs):
        if path.name == "update-activity.lock":
            (path.parent / "update-registration.json").write_text(
                json.dumps(
                    {
                        "installation": str(tmp_path / "replacement"),
                    }
                )
            )
        with original(path, exclusive=exclusive, **kwargs) as stream:
            yield stream

    monkeypatch.setattr(update_admission, "_file_lock", changed)
    with pytest.raises(update_admission.MaintenanceInProgress, match="registration changed"):
        public_wrapper.run([sys.executable, "-c", 'print("must not run")'], timeout=5)
    assert public_wrapper.events == []
    assert not (public_wrapper.installation / "windows-jobs").exists()


def test_windows_without_update_registration_preserves_legacy_dispatch(tmp_path, monkeypatch):
    from bubo import paths, subproc

    monkeypatch.setattr(subproc.sys, "platform", "win32")
    monkeypatch.setattr(paths, "DB", tmp_path / "bubo.db")
    monkeypatch.setattr(
        bounded, "run_bounded_windows", lambda *a, **kw: pytest.fail("Unregistered native dispatch")
    )
    observed = []

    def popen(args, **kwargs):
        observed.append((args, kwargs))
        return SimpleNamespace(returncode=3, communicate=lambda timeout: ("legacy output", None))

    monkeypatch.setattr(subproc.subprocess, "Popen", popen)
    result = subproc.run_bounded(["command"], timeout=2)
    assert result.returncode == 3
    assert result.stdout == "legacy output"
    assert observed[0][1]["start_new_session"] is True


def test_guardian_preserves_unicode_under_legacy_encoding(guardian, monkeypatch):
    import io

    real_wrapper = io.TextIOWrapper
    monkeypatch.setattr(
        io, "TextIOWrapper", lambda *a, **kw: real_wrapper(*a, **({"encoding": "cp1252"} | kw))
    )
    text = "Ą café 漢字 🦉"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    script = f"print({text!a}, flush=True)"
    result = bounded.run_bounded_windows([sys.executable, "-c", script], env=env, timeout=5)
    assert result.returncode == 0
    assert result.stdout == text + "\n"
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        bounded.run_bounded_windows(
            [sys.executable, "-c", script + "; import time; time.sleep(30)"], env=env, timeout=1
        )
    assert caught.value.output == text + "\n"
    assert env["PYTHONIOENCODING"] == "cp1252"
