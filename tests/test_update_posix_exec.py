"""Real-process launch gate races; no package installation or telemetry."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from bubo.update_identity import atomic_json
from bubo.update_installers import writer_quiescent
from bubo.update_posix_exec import GUARDIAN, PROTOCOL, cancel_unstarted, gate_path, launch_gate

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX launch gate")


@pytest.fixture
def launch(tmp_path):
    writer = tmp_path / "writer.json"
    expected = {"state": "starting", "protocol": PROTOCOL, "nonce": uuid.uuid4().hex}
    gate_path(writer).touch(mode=0o600)
    atomic_json(writer, expected)
    marker = tmp_path / "executed"
    processes = []

    def spawn(code=GUARDIAN):
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                code,
                str(writer),
                expected["nonce"],
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                str(marker),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    yield writer, expected, marker, spawn
    for process in processes:
        if process.poll() is None:
            os.killpg(process.pid, 9)
        process.wait(timeout=5)


def wait_file(path: Path, process):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert process.poll() is None
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_cancellation_before_spawn_prevents_command(launch):
    writer, expected, marker, spawn = launch
    assert cancel_unstarted(writer, expected)
    assert spawn().wait(timeout=5) != 0
    assert not marker.exists()
    assert writer_quiescent(writer)


def test_guardian_wins_and_publishes_before_command(launch):
    writer, expected, marker, spawn = launch
    process = spawn()
    assert process.wait(timeout=5) == 0
    record = json.loads(writer.read_text())
    assert record["state"] == "running"
    assert record["pid"] == process.pid
    assert record["start"]
    assert marker.exists()
    assert not cancel_unstarted(writer, expected)
    assert writer_quiescent(writer)


def test_recovery_does_not_cancel_while_gate_is_held(launch):
    writer, expected, marker, spawn = launch
    with launch_gate(writer):
        assert not cancel_unstarted(writer, expected)
        assert spawn().wait(timeout=5) != 0
        assert json.loads(writer.read_text()) == expected
    assert cancel_unstarted(writer, expected)
    assert not marker.exists()


def test_late_guardian_rejects_replaced_gate_inode(launch):
    writer, expected, marker, spawn = launch
    ready, resume = writer.parent / "opened", writer.parent / "resume"
    pause = f"\npathlib.Path({str(ready)!r}).touch()\nimport time\nwhile not pathlib.Path({str(resume)!r}).exists(): time.sleep(.01)\n"
    code = GUARDIAN.replace("fcntl.flock(fd,", pause + "fcntl.flock(fd,", 1)
    process = spawn(code)
    wait_file(ready, process)
    assert cancel_unstarted(writer, expected)
    gate_path(writer).unlink()
    gate_path(writer).touch(mode=0o600)
    atomic_json(writer, expected)  # Even accidentally reused nonce cannot bypass inode proof.
    resume.touch()
    assert process.wait(timeout=5) != 0
    assert not marker.exists()
    assert json.loads(writer.read_text()) == expected


def test_old_guardian_rejects_new_attempt_nonce(launch):
    writer, expected, marker, spawn = launch
    assert cancel_unstarted(writer, expected)
    atomic_json(writer, {**expected, "nonce": uuid.uuid4().hex})
    assert spawn().wait(timeout=5) != 0
    assert not marker.exists()


def test_crash_after_running_publication_before_exec(launch):
    writer, expected, marker, spawn = launch
    ready = writer.parent / "published"
    pause = f"pathlib.Path({str(ready)!r}).touch()\nimport time; time.sleep(60)\n"
    process = spawn(GUARDIAN.replace("os.execvpe(command[0]", pause + "os.execvpe(command[0]"))
    wait_file(ready, process)
    assert json.loads(writer.read_text())["state"] == "running"
    assert not cancel_unstarted(writer, expected)
    process.kill()
    process.wait(timeout=5)
    assert writer_quiescent(writer)
    assert not marker.exists()


def test_failed_cancellation_write_never_proves_quiescence(launch, monkeypatch):
    from bubo import update_posix_exec

    writer, expected, marker, spawn = launch

    def fail(*_args):
        raise OSError("disk failure")

    monkeypatch.setattr(update_posix_exec, "atomic_json", fail)
    assert not cancel_unstarted(writer, expected)
    assert json.loads(writer.read_text()) == expected
    assert spawn().wait(timeout=5) == 0
    assert marker.exists()


def test_guardian_linux_identity_matches_recovery_format(launch, monkeypatch):
    from bubo.update_installers import _start_identity

    writer, _expected, marker, spawn = launch
    proc_stat = "123 (python worker) " + " ".join(["S"] + ["0"] * 18 + ["987654"])
    prefix = f"""
import pathlib, sys
sys.platform = 'linux'
original_read = pathlib.Path.read_text
def proc_read(path, *args, **kwargs):
    if str(path) == '/proc/sys/kernel/random/boot_id':
        return 'test-boot-id'
    if str(path).startswith('/proc/') and str(path).endswith('/stat'):
        return {proc_stat!r}
    return original_read(path, *args, **kwargs)
pathlib.Path.read_text = proc_read
"""
    process = spawn(prefix + GUARDIAN)
    assert process.wait(timeout=5) == 0
    assert marker.exists()
    record = json.loads(writer.read_text())
    original_read = Path.read_text

    def proc_read(path, *args, **kwargs):
        if str(path) == "/proc/sys/kernel/random/boot_id":
            return "test-boot-id"
        if str(path).startswith("/proc/") and str(path).endswith("/stat"):
            return proc_stat
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        patch.setattr(Path, "read_text", proc_read)
        assert record["start"] == _start_identity(process.pid) == "proc:test-boot-id:987654"
