import ctypes as C
import subprocess

import pytest

from bubo import update_windows_process as windows


class Kernel:
    def __init__(self, monkeypatch, fail=None):
        self.error = 0
        self.fail = fail
        self.calls = []
        self.closed = []
        self.duplicates = []
        self.attributes = {}
        self.active = 1
        self.exited = False
        self.linger = False
        monkeypatch.setattr(windows, "_api", lambda: self)
        monkeypatch.setattr(C, "get_last_error", lambda: self.error, raising=False)
        monkeypatch.setattr(
            C, "set_last_error", lambda value: setattr(self, "error", value), raising=False
        )

    def CreateJobObjectW(self, security, name):
        self.calls.append("job")
        self.error = 183 if self.fail == "existing" else 0
        return 0 if self.fail == "job" else 100

    def SetInformationJobObject(self, job, kind, limits, size):
        assert job == 100
        assert kind == 9
        assert limits._obj.basic.flags == 0x2000
        self.calls.append("limits")
        return self.fail != "limits"

    def GetCurrentProcess(self):
        return 1000

    def IsProcessInJob(self, process, job, result):
        assert process == 1000
        assert job == 100
        result._obj.value = self.fail != "outside"
        return self.fail != "membership"

    def DuplicateHandle(self, parent, source, target, destination, rights, inherit, flags):
        assert parent == target == 1000
        assert inherit is True
        assert flags == 2
        if self.fail == "duplicate" and self.duplicates:
            return False
        value = source + 100
        destination._obj.value = value
        self.duplicates.append(value)
        return True

    def InitializeProcThreadAttributeList(self, pointer, count, flags, size):
        assert count == 2
        self.calls.append("attribute-init" if pointer is not None else "attribute-size")
        if pointer is None:
            size._obj.value = 128
            self.error = 122
            return False
        return self.fail != "initialize"

    def UpdateProcThreadAttribute(self, attributes, flags, key, value, size, previous, returned):
        self.calls.append("attribute")
        self.attributes[key] = list(value)
        return self.fail != "attribute"

    def DeleteProcThreadAttributeList(self, pointer):
        self.calls.append("attribute-delete")

    def CreateProcessW(
        self,
        executable,
        command,
        process_security,
        thread_security,
        inherit,
        flags,
        env,
        cwd,
        startup,
        info,
    ):
        self.calls.append("create")
        assert executable == "/python.exe"
        assert command.value == '/python.exe "a b"'
        assert inherit is True
        assert flags == 0x00080408
        assert startup._obj.startup.cb == C.sizeof(windows._StartupEx)
        assert startup._obj.startup.flags == 0x100
        assert self.attributes == {0x2000D: [100], 0x20002: [101, 102, 103]}
        assert startup._obj.startup.stdin == 101
        assert startup._obj.startup.stdout == 102
        assert startup._obj.startup.stderr == 103
        if self.fail == "create":
            return False
        info._obj.process = 200
        info._obj.thread = 201
        info._obj.pid = 123
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True

    def WaitForSingleObject(self, handle, timeout):
        assert handle == 200
        return 0 if self.exited else 258

    def GetExitCodeProcess(self, handle, result):
        result._obj.value = 0
        return True

    def GetProcessTimes(self, handle, created, exited, system, user):
        assert handle == 200
        created._obj.low = 13
        created._obj.high = 2
        return self.fail != "times"

    def QueryInformationJobObject(self, job, kind, result, size, returned):
        assert job == 100
        if kind == 3:
            windows.DWORD.from_buffer(result, 4).value = 0
        elif kind == 9:
            result._obj.basic.flags = {
                "no-kill": 0,
                "breakaway": 0x2800,
                "silent-breakaway": 0x3000,
            }.get(self.fail, 0x2000)
        else:
            assert kind == 1
            result._obj.active = self.active
        return self.fail != "query"

    def TerminateJobObject(self, job, status):
        assert job == 100
        self.calls.append("terminate")
        if not self.linger:
            self.active = 0
            self.exited = True
        return self.fail != "terminate"

    def OpenJobObjectW(self, rights, inherit, name):
        assert rights in {4, 12}
        assert inherit is False
        self.calls.append("open")
        if self.fail in {"missing", "denied"}:
            self.error = 2 if self.fail == "missing" else 5
            return 0
        return 100


def spawn():
    return windows.JobProcess(
        ["/python.exe", "a b"],
        name=windows.new_job_name(),
        handles=(1, 2, 3),
        env={"SECRET": "memory-only"},
    )


def test_native_structures_have_windows_64bit_layout():
    if C.sizeof(C.c_void_p) != 8:
        pytest.skip("64-bit ABI layout assertions")
    assert C.sizeof(windows._Limits) == 64
    assert C.sizeof(windows._ExtendedLimits) == 144
    assert C.sizeof(windows._Accounting) == 48
    assert C.sizeof(windows._Startup) == 104
    assert C.sizeof(windows._StartupEx) == 112
    assert C.sizeof(windows._ProcessInfo) == 24


def test_job_and_handle_list_are_applied_atomically_before_child_creation(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    assert child.pid == 123
    assert api.calls.index("limits") < api.calls.index("attribute") < api.calls.index("create")
    assert api.closed == [201, 101, 102, 103]
    assert child.poll() is None
    assert not child.quiescent()
    child.close()
    assert api.calls[-1] == "terminate"
    assert api.closed[-2:] == [100, 200]
    child.close()
    assert api.closed.count(100) == 1


@pytest.mark.parametrize(
    "failure", ["job", "existing", "limits", "duplicate", "initialize", "attribute", "create"]
)
def test_spawn_failure_releases_handles_without_uncontained_child(monkeypatch, failure):
    api = Kernel(monkeypatch, failure)
    with pytest.raises(OSError, match="Windows"):
        spawn()
    assert all(handle in api.closed for handle in api.duplicates)
    assert api.closed.count(100) == (0 if failure == "job" else 1)
    if failure not in {"create"}:
        assert "create" not in api.calls
    if failure in {"attribute", "create"}:
        assert "attribute-delete" in api.calls


def test_root_exit_does_not_hide_surviving_descendants(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    api.exited = True
    assert child.wait(timeout=0) == 0
    assert not child.quiescent()
    child.close()
    assert "terminate" in api.calls
    assert api.active == 0


def test_job_wait_timeout_keeps_ownership_until_explicit_close(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    with pytest.raises(subprocess.TimeoutExpired):
        child.wait(timeout=0)
    assert 100 not in api.closed
    child.close()


@pytest.mark.parametrize("failure", [None, "missing", "denied", "query", "terminate"])
def test_recovery_uses_job_membership_not_parent_exit(monkeypatch, failure):
    api = Kernel(monkeypatch, failure)
    if failure in {"denied", "query", "terminate"}:
        with pytest.raises(OSError, match="Windows"):
            windows.recover_job(windows.new_job_name())
    else:
        windows.recover_job(windows.new_job_name())
    if failure in {"missing", "denied"}:
        assert api.closed == []
        assert "terminate" not in api.calls
    else:
        assert api.closed == [100]


def test_recovery_times_out_if_descendants_do_not_terminate(monkeypatch):
    api = Kernel(monkeypatch)
    api.linger = True
    with pytest.raises(TimeoutError, match="descendants"):
        windows.recover_job(windows.new_job_name(), timeout=0)
    assert api.closed == [100]


@pytest.mark.parametrize("name", ["OtherJob", "Global\\BuboUpdate-../../elsewhere", None])
def test_unowned_job_name_is_rejected_before_native_api(monkeypatch, name):
    api = Kernel(monkeypatch)
    with pytest.raises(ValueError, match="job name"):
        windows.recover_job(name)
    assert not api.calls


def test_job_ownership_must_publish_before_any_child_can_run(monkeypatch):
    api = Kernel(monkeypatch)

    def fail_publication():
        raise OSError("journal unavailable")

    with pytest.raises(OSError, match="journal unavailable"):
        windows.JobProcess(
            ["/python.exe", "a b"],
            name=windows.new_job_name(),
            handles=(1, 2, 3),
            env={},
            on_job_created=fail_publication,
        )
    assert "create" not in api.calls
    assert api.closed == [100]


def test_capture_drains_only_after_whole_job_exits(monkeypatch):
    import os
    import sys
    from types import SimpleNamespace

    api = Kernel(monkeypatch)
    api.active = 0
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    events = []

    class Captured:
        job = 100
        pid = 123

        def __init__(self, args, *, handles, on_job_created, **kwargs):
            self.api = api
            on_job_created()
            os.write(handles[1], b"captured stdout")

        def wait(self, timeout):
            events.append("wait")
            return 0

        def close(self):
            events.append("close")

    monkeypatch.setattr(windows, "JobProcess", Captured)
    assert (
        windows.run_capture(
            ["/python.exe"],
            name=windows.new_job_name(),
            env={},
            timeout=1,
            on_job_created=lambda: events.append("created"),
            on_started=lambda pid: events.append("started"),
        )
        == b"captured stdout"
    )
    assert events == ["created", "started", "wait", "close"]


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "denied",
        "membership",
        "outside",
        "query",
        "no-kill",
        "breakaway",
        "silent-breakaway",
    ],
)
def test_guardian_cannot_acknowledge_unverified_containment(monkeypatch, failure):
    api = Kernel(monkeypatch, failure)
    with pytest.raises(OSError, match="Windows"):
        windows.GuardianJob(windows.new_job_name())
    assert api.closed == ([] if failure in {"missing", "denied"} else [100])


def test_guardian_counts_descendants_and_rechecks_own_membership(monkeypatch):
    api = Kernel(monkeypatch)
    guardian = windows.GuardianJob(windows.new_job_name())
    assert guardian.alone()
    api.active = 2
    assert not guardian.alone()
    api.active = 0
    assert not guardian.alone()
    api.fail = "outside"
    with pytest.raises(OSError, match="membership changed"):
        guardian.alone()
    assert api.closed == []


def test_guardian_exit_retains_handle_and_never_returns_normally(monkeypatch):
    api = Kernel(monkeypatch)
    guardian = windows.GuardianJob(windows.new_job_name())
    calls = []
    monkeypatch.setattr(api, "ExitProcess", calls.append, raising=False)
    with pytest.raises(RuntimeError, match="unexpectedly returned"):
        guardian.exit(7)
    assert calls == [7]
    assert api.closed == []
    assert guardian.job == 100


def test_failed_handoff_validation_keeps_parent_kill_authority(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()

    def invalid(actual):
        assert actual is child
        raise OSError("unacknowledged")

    with pytest.raises(OSError, match="unacknowledged"):
        child.detach(invalid)
    assert child.job == 100
    assert child.process == 200
    child.close()
    assert "terminate" in api.calls


def test_validated_handoff_disarms_close_and_destructor(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    validated = []
    child.detach(lambda actual: validated.append((actual.pid, actual.job, actual.process)))
    assert validated == [(123, 100, 200)]
    child.close()
    child.__del__()
    assert "terminate" not in api.calls
    assert api.closed.count(100) == api.closed.count(200) == 1
    with pytest.raises(OSError, match="relinquished"):
        child.detach(lambda actual: pytest.fail("Repeated acknowledgement"))


@pytest.mark.parametrize("failed_handle", [100, 200])
def test_handoff_handle_close_failure_keeps_remaining_handle(monkeypatch, failed_handle):
    api = Kernel(monkeypatch)
    child = spawn()
    original = api.CloseHandle
    monkeypatch.setattr(
        api, "CloseHandle", lambda handle: False if handle == failed_handle else original(handle)
    )
    if failed_handle == 100:
        with pytest.raises(OSError, match="handoff handle close"):
            child.detach(lambda actual: None)
    else:
        # Committed ownership is success even if a local handle needs later cleanup.
        child.detach(lambda actual: None)
    assert child.job == (100 if failed_handle == 100 else None)
    assert child.process == 200
    monkeypatch.setattr(api, "CloseHandle", original)
    child.close()
    assert ("terminate" in api.calls) == (failed_handle == 100)


def test_retained_process_handle_identifies_even_an_exited_child(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    assert child.identity() == "windows:8589934605"
    api.exited = True
    assert child.wait(timeout=0) == 0
    assert child.identity() == "windows:8589934605"
    api.fail = "times"
    with pytest.raises(OSError, match="identity is unavailable"):
        child.identity()
    child.close()
    with pytest.raises(OSError, match="relinquished"):
        child.identity()


@pytest.mark.parametrize("active", [0, 2])
def test_read_only_job_quiescence_uses_query_access_and_never_terminates(monkeypatch, active):
    native = Kernel(monkeypatch)

    def open_job(access, inherit, name):
        assert access == 0x0004
        assert not inherit
        return 100

    native.OpenJobObjectW = open_job
    native.active = active
    assert windows.job_quiescent(windows.new_job_name()) is (active == 0)
    assert native.closed == [100]
    assert "terminate" not in native.calls


@pytest.mark.parametrize("wait_result", [0, 258])
def test_termination_waits_for_retained_member_exit_and_preserves_job_on_timeout(
    monkeypatch, wait_result
):
    api = Kernel(monkeypatch)
    monkeypatch.setattr(windows, "_member_handles", lambda kernel, job: [901])
    waits = []

    def wait(handle, timeout):
        waits.append(handle)
        return wait_result

    monkeypatch.setattr(api, "WaitForSingleObject", wait)
    if wait_result:
        with pytest.raises(TimeoutError, match="finished exiting"):
            windows._terminate_job(api, 100, 1)
    else:
        windows._terminate_job(api, 100, 1)
    assert waits == [901]
    assert 901 in api.closed
    assert 100 not in api.closed


def test_close_retains_ownership_until_empty_job_root_exit(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    monkeypatch.setattr(windows, "_active", lambda *_: 0)
    with pytest.raises(TimeoutError):
        child.close()
    assert child.job == 100
    assert child.process == 200
    assert 100 not in api.closed
    assert 200 not in api.closed
    api.exited = True
    child.close()
    assert child.job is None
    assert child.process is None


def test_close_waits_for_pinned_descendants_after_root_exit(monkeypatch):
    api = Kernel(monkeypatch)
    child = spawn()
    monkeypatch.setattr(windows, "_member_handles", lambda *_: [31])
    child.retain_members()
    monkeypatch.setattr(windows, "_active", lambda *_: 0)
    api.exited = True
    monkeypatch.setattr(api, "WaitForSingleObject", lambda handle, _: 258 if handle == 31 else 0)
    with pytest.raises(TimeoutError):
        child.close()
    assert child.job == 100
    assert child._stop_handles == [31]
    assert 31 not in api.closed
    monkeypatch.setattr(api, "WaitForSingleObject", lambda *_: 0)
    child.close()
    assert 31 in api.closed
