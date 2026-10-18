"""Resident process boundary and supervisor state tests, without native enablement."""

import errno
import inspect
import json
import os
import sys
from types import SimpleNamespace

import pytest

from bubo import update_supervisor as supervisor
from bubo import update_windows_resident as resident


@pytest.fixture
def native_boundary(tmp_path, monkeypatch):
    signature = inspect.signature(resident.JobProcess)
    calls, children, streams = [], [], []
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(resident, "sys", SimpleNamespace())
    for name in ("stdin", "stdout", "stderr"):
        stream = (tmp_path / name).open("w+b", buffering=0)
        streams.append(stream)
        monkeypatch.setattr(resident.sys, name, stream, raising=False)

    class Native:
        pid = 123
        returncode = None

        def __init__(self, args, **kwargs):
            signature.bind(args, **kwargs)
            self.name = kwargs["name"]
            self.descriptors = [os.dup(handle) for handle in kwargs["handles"]]
            self.closed = False
            self.quiet = False
            self.fail_close = False
            self.on_close = lambda: None
            children.append(self)
            calls.append((args, kwargs))
            if kwargs["on_job_created"]:
                kwargs["on_job_created"]()

        def retain_members(self):
            pass

        def identity(self):
            return "windows:123-start"

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def quiescent(self):
            return self.quiet

        def close(self):
            self.on_close()
            if self.fail_close:
                raise TimeoutError("whole job is not quiet")
            self.closed = self.quiet = True

    monkeypatch.setattr(resident, "JobProcess", Native)
    yield SimpleNamespace(calls=calls, children=children, streams=streams)
    for child in children:
        for descriptor in child.descriptors:
            os.close(descriptor)
    for stream in streams:
        stream.close()


def test_resident_forwards_stdio_and_exact_launch_configuration(native_boundary):
    created = []
    child = resident.WindowsResidentProcess(
        ["python", "-m", "bubo", "service", "run"],
        name="Global\\BuboUpdate-" + "a" * 32,
        cwd="/workspace",
        env={"CONTEXT": "in-memory"},
        on_job_created=lambda: created.append(True),
    )
    assert created == [True]
    assert child.stdin is None
    assert child.pid == 123
    assert child.identity() == "windows:123-start"
    assert native_boundary.calls[0][1]["env"] == {"CONTEXT": "in-memory"}
    os.write(native_boundary.children[0].descriptors[1], b"resident output")
    native_boundary.streams[1].seek(0)
    assert native_boundary.streams[1].read() == b"resident output"
    assert not child.quiescent()
    child.close()
    assert child.quiescent()


def test_probe_has_private_input_and_does_not_write_client_stdout(native_boundary):
    child = resident.WindowsResidentProcess(
        ["python", "-m", "bubo.mcp_server"],
        name="Global\\BuboUpdate-" + "b" * 32,
        cwd="/workspace",
        env={},
        probe=True,
    )
    native = native_boundary.children[0]
    child.stdin.write(b"initialize\n")
    assert os.read(native.descriptors[0], 11) == b"initialize\n"
    os.write(native.descriptors[1], b"must not reach client")
    assert native_boundary.streams[1].tell() == 0
    native.on_close = lambda: (
        pytest.fail("Probe input released before tree stop") if child.stdin.closed else None
    )
    native.fail_close = True
    with pytest.raises(TimeoutError, match="whole job is not quiet"):
        child.close()
    assert not child.stdin.closed
    native.fail_close = False
    child.close()
    assert child.stdin.closed


@pytest.fixture
def windows_supervisor(tmp_path, monkeypatch):
    installation, state = tmp_path / "installation", tmp_path / "state"
    installation.mkdir()
    state.mkdir()
    # Local proxy changes supervisor platform selection without changing pathlib's host.
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(supervisor, "_start_identity", lambda pid: f"identity-{pid}")
    from bubo import update_windows_broker

    monkeypatch.setattr(
        update_windows_broker,
        "WorkerBroker",
        lambda *args: SimpleNamespace(
            environment=lambda: {},
            bind=lambda job: None,
            close=lambda: None,
        ),
    )
    monkeypatch.setenv("PRIVATE_CONFIG", "never persist")
    return supervisor.ResidentSupervisor(
        installation,
        state,
        "service",
        ["python", "-m", "bubo", "service", "run"],
    )


def test_supervisor_publishes_creation_before_child_and_uses_existing_readiness(
    windows_supervisor, monkeypatch
):
    parent = windows_supervisor
    sequence = []

    class Process:
        job = 456
        pid = 123
        stdin = None

        def __init__(self, args, *, name, cwd, env, probe, control_input, on_job_created):
            before = json.loads((parent.directory / "owner.json").read_text())
            assert before["child"] == {"job": name, "created": False}
            on_job_created()
            assert (
                json.loads((parent.directory / "owner.json").read_text())["child"]["created"]
                is True
            )
            sequence.append("created")
            self.name = name

        def poll(self):
            return 0

        def retain_members(self):
            pass

        def identity(self):
            return "identity-123"

        def close(self):
            assert (
                json.loads((parent.directory / "owner.json").read_text())["child_state"]
                == "running"
            )
            sequence.append("stopped")

    monkeypatch.setattr(resident, "WindowsResidentProcess", Process)
    monkeypatch.setattr(
        supervisor,
        "ResidentReadiness",
        lambda *args, **kwargs: SimpleNamespace(
            environment=lambda env: env,
            bind=lambda child: sequence.append("bound"),
            wait=lambda: sequence.append("ready"),
        ),
    )
    parent._launch(outcome="updated")
    assert sequence == ["created", "bound", "ready"]
    record = json.loads((parent.directory / "owner.json").read_text())
    assert record["process_backend"] == "windows-job-v1"
    assert record["child"]["identity"] == "identity-123"
    assert "PRIVATE_CONFIG" not in (parent.directory / "owner.json").read_text()
    assert parent._stop()
    assert sequence == ["created", "bound", "ready", "stopped"]
    assert json.loads((parent.directory / "owner.json").read_text())["child_state"] == "stopped"
    assert not (parent.installation / "windows-jobs").exists()


def test_unproven_resident_stop_never_publishes_stopped(windows_supervisor):
    parent = windows_supervisor
    name = "Global\\BuboUpdate-" + "c" * 32
    parent.record.update(child={"job": name, "created": True}, child_state="running")
    parent._publish()

    def fail():
        raise TimeoutError("unproven descendant shutdown")

    parent.child = SimpleNamespace(
        name=name, close=fail, poll=lambda: 0, retain_members=lambda: None
    )
    before = (parent.directory / "owner.json").read_bytes()
    with pytest.raises(TimeoutError, match="unproven descendant shutdown"):
        parent._stop()
    assert (parent.directory / "owner.json").read_bytes() == before
    assert parent.child is not None


@pytest.mark.parametrize("created", [False, True])
def test_constructor_cleanup_uses_only_confirmed_job_ownership(
    windows_supervisor, monkeypatch, created
):
    from bubo import update_windows_process

    parent = windows_supervisor
    name = "Global\\BuboUpdate-" + "d" * 32
    parent.record.update(child={"job": name, "created": created}, child_state="launching")
    recovered = []
    monkeypatch.setattr(update_windows_process, "recover_job", recovered.append)
    assert parent._stop()
    assert recovered == ([name] if created else [])


@pytest.mark.parametrize("created", [False, True])
def test_orphan_recovery_requires_dead_parent_and_confirmed_creation(monkeypatch, created):
    from bubo import update_windows_process

    name = "Global\\BuboUpdate-" + "e" * 32
    recovered = []
    monkeypatch.setattr(update_windows_process, "recover_job", recovered.append)
    record = {
        "pid": 123,
        "identity": "parent-start",
        "process_backend": "windows-job-v1",
        "child_state": "launching",
        "child": {"job": name, "created": created},
    }
    monkeypatch.setattr(supervisor, "_start_identity", lambda pid: "parent-start")
    assert not supervisor._orphan_quiescent(record)
    assert recovered == []
    monkeypatch.setattr(supervisor, "_start_identity", lambda pid: None)
    assert supervisor._orphan_quiescent(record)
    assert recovered == ([name] if created else [])


def test_probe_constructor_failure_closes_private_descriptors(native_boundary, monkeypatch):
    descriptors = []
    real_pipe = os.pipe

    def pipe():
        pair = real_pipe()
        descriptors.extend(pair)
        return pair

    def fail(*args, **kwargs):
        raise OSError("CreateProcess failed")

    monkeypatch.setattr(resident.os, "pipe", pipe)
    monkeypatch.setattr(resident, "JobProcess", fail)
    with pytest.raises(OSError, match="CreateProcess failed"):
        resident.WindowsResidentProcess(["python"], name="job", cwd=".", env={}, probe=True)
    for descriptor in descriptors:
        with pytest.raises(OSError, match=r".+") as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


def test_missing_standard_streams_use_live_null_handles(native_boundary, monkeypatch):
    monkeypatch.setattr(resident, "sys", SimpleNamespace(stdin=None, stdout=None, stderr=None))
    process = resident.WindowsResidentProcess(["python"], name="job", cwd=".", env={})
    # The signature-checked native boundary verifies each handle while creating
    # the child; temporary parent descriptors must be closed after creation.
    for descriptor in native_boundary.calls[0][1]["handles"]:
        with pytest.raises(OSError, match=r".+") as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
    process.close()


def test_failed_creation_publication_never_starts_child(windows_supervisor, monkeypatch):
    parent = windows_supervisor
    from bubo import update_windows_process as native

    launches = []
    original_publish = parent._publish

    def publish():
        child = parent.record.get("child") or {}
        if child.get("created"):
            raise OSError("ownership write failed")
        original_publish()

    def process(*args, **kwargs):
        kwargs["on_job_created"]()
        launches.append("started")
        pytest.fail("child creation must not follow failed ownership publication")

    monkeypatch.setattr(parent, "_publish", publish)
    monkeypatch.setattr(resident, "WindowsResidentProcess", process)
    with pytest.raises(OSError, match="ownership write failed"):
        parent._launch()
    assert not launches
    disk = json.loads((parent.directory / "owner.json").read_text())
    assert disk["child"]["created"] is False
    assert parent.record["child"]["created"] is True
    recovered = []
    monkeypatch.setattr(parent, "_publish", original_publish)
    monkeypatch.setattr(native, "recover_job", lambda name: recovered.append(name) or True)
    parent._stop()
    assert recovered == [disk["child"]["job"]]
    assert parent.record["child_state"] == "stopped"


@pytest.mark.parametrize("broker_failure", [False, True])
def test_broker_retires_before_resident_job(windows_supervisor, broker_failure):
    parent = windows_supervisor
    events = []
    job = "Global\\BuboUpdate-" + "a" * 32

    def close_broker():
        events.append("broker")
        if broker_failure:
            raise TimeoutError("pending launch")

    parent.worker_broker = SimpleNamespace(close=close_broker)
    parent.child = SimpleNamespace(
        name=job, close=lambda: events.append("job"), poll=lambda: 0, retain_members=lambda: None
    )
    parent.record.update(child_state="running", child={"job": job, "created": True})
    if broker_failure:
        with pytest.raises(TimeoutError, match="pending launch"):
            parent._stop()
        assert events == ["broker"]
        assert parent.record["child_state"] == "running"
    else:
        parent._stop()
        assert events == ["broker", "job"]
        assert parent.record["child_state"] == "stopped"


def test_natural_resident_exit_closes_native_handles_after_broker(windows_supervisor, monkeypatch):
    parent = windows_supervisor
    events = []
    parent.worker_broker = SimpleNamespace(close=lambda: events.append("broker"))
    parent.child = SimpleNamespace(
        wait=lambda timeout: events.append("wait"),
        close=lambda: events.append("handles"),
    )
    monkeypatch.setattr(parent, "_quiescent", lambda: True)
    assert parent._reap_child()
    assert events == ["wait", "broker", "handles"]
    assert parent.child is None
    assert parent.record["child_state"] == "stopped"


def test_exact_service_child_consumes_shared_cooperative_stop(tmp_path, monkeypatch):
    from bubo import paths, service, update_platform

    pid_path = tmp_path / "bubo-service.pid"
    monkeypatch.setattr(paths, "SERVICE_PID", pid_path)
    pid_path.write_text(json.dumps({"pid": 123, "token": "generation-token"}))
    child = SimpleNamespace(pid=123, identity=lambda: "windows:456", job=99)
    monkeypatch.setattr(update_platform, "windows_process_identity", lambda pid: "windows:456")
    from contextlib import nullcontext

    from bubo import update_windows_process

    monkeypatch.setattr(update_windows_process, "verified_job_member", lambda *args: nullcontext())
    assert resident.request_service_stop(tmp_path, child)
    assert service._consume_stop_request("generation-token")
    assert not service._consume_stop_request("generation-token")


@pytest.mark.parametrize(("pid", "identity"), [(124, "windows:456"), (123, "windows:other")])
def test_stale_service_identity_never_requests_stop(tmp_path, monkeypatch, pid, identity):
    from bubo import update_platform

    pid_path = tmp_path / "bubo-service.pid"
    pid_path.write_text(json.dumps({"pid": pid, "token": "generation-token"}))
    child = SimpleNamespace(pid=123, identity=lambda: "windows:456", job=99)
    monkeypatch.setattr(update_platform, "windows_process_identity", lambda pid: identity)
    from bubo import update_windows_process

    def reject(*args):
        raise OSError("not in resident Job")

    monkeypatch.setattr(update_windows_process, "verified_job_member", reject)
    assert not resident.request_service_stop(tmp_path, child)
    assert list(tmp_path.iterdir()) == [pid_path]


@pytest.mark.parametrize("finishes", [False, True])
@pytest.mark.parametrize("kind", ["service", "mcp-http"])
def test_resident_stop_precedes_broker_retirement_and_bounded_job_cleanup(
    windows_supervisor,
    monkeypatch,
    finishes,
    kind,
):
    parent = windows_supervisor
    events = []
    job = "Global\\BuboUpdate-" + "b" * 32
    parent.kind = parent.record["kind"] = kind
    checks = iter([False, True]) if finishes else iter([False])
    clock = iter([0, 1] if finishes else [0, 4])
    parent.worker_broker = SimpleNamespace(close=lambda: events.append("broker"))
    parent.child = SimpleNamespace(
        name=job,
        poll=lambda: None,
        quiescent=lambda: next(checks),
        retain_members=lambda: events.append("pin"),
        close=lambda: events.append("job"),
        request_stop=lambda: events.append("request") or True,
    )
    parent.record.update(child_state="running", child={"job": job, "created": True})
    monkeypatch.setattr(
        resident, "request_service_stop", lambda *args: events.append("request") or True
    )
    monkeypatch.setattr(
        supervisor,
        "time",
        SimpleNamespace(
            monotonic=lambda: next(clock),
            sleep=lambda seconds: events.append("wait"),
        ),
    )
    assert parent._stop()
    assert events == (
        ["pin", "request", "broker", "wait", "job"]
        if finishes
        else ["pin", "request", "broker", "job"]
    )
    assert parent.record["child_state"] == "stopped"


def test_http_control_pipe_preserves_stdout_and_closes_cooperatively(native_boundary):
    child = resident.WindowsResidentProcess(
        ["python"],
        name="job",
        cwd=".",
        env={},
        control_input=True,
    )
    _, parameters = native_boundary.calls[0]
    assert parameters["env"][resident.CONTROL_INPUT] == "1"
    native = native_boundary.children[0]
    os.write(native.descriptors[1], b"visible output")
    native_boundary.streams[1].seek(0)
    assert native_boundary.streams[1].read() == b"visible output"
    assert child.request_stop()
    assert os.read(native.descriptors[0], 1) == b""
    assert not native.closed
    child.close()


def test_http_monitor_requires_explicit_control_marker(monkeypatch):
    monkeypatch.delenv(resident.CONTROL_INPUT, raising=False)
    assert resident.http_stop_event() is None


def test_http_monitor_waits_for_eof_and_ignores_pipe_data(monkeypatch):
    reader_fd, writer_fd = os.pipe()
    with os.fdopen(reader_fd, "rb", buffering=0) as reader:
        writer = os.fdopen(writer_fd, "wb", buffering=0)
        try:
            monkeypatch.setenv(resident.CONTROL_INPUT, "1")
            monkeypatch.setattr(resident, "sys", SimpleNamespace(stdin=reader))
            stopped = resident.http_stop_event()
            assert resident.CONTROL_INPUT not in os.environ
            writer.write(b"ignored")
            assert not stopped.is_set()
        finally:
            writer.close()
        assert stopped.wait(2)


def test_missing_http_control_stream_requests_exit_without_thread_error(monkeypatch):
    monkeypatch.setenv(resident.CONTROL_INPUT, "1")
    monkeypatch.setattr(resident, "sys", SimpleNamespace(stdin=None))
    assert resident.http_stop_event().wait(2)
