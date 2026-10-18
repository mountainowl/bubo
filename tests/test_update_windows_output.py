"""Verified handle transfer at the resident/worker launch boundary."""

import ctypes as C
import os
import sys
from types import SimpleNamespace

import pytest

from bubo import update_windows_process as windows


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    source = (tmp_path / "worker.log").open("wb", buffering=0)
    state = SimpleNamespace(
        closed=[],
        duplicates=[],
        identity="windows:100",
        member=True,
        running=True,
        duplicate=True,
        conversions=[],
    )

    class Kernel:
        def OpenProcess(self, rights, inherit, pid):
            assert (rights, inherit, pid) == (0x101040, False, 42)
            return 50

        def GetCurrentProcess(self):
            return 60

        def WaitForSingleObject(self, process, timeout):
            assert (process, timeout) == (50, 0)
            return 258 if state.running else 0

        def IsProcessInJob(self, process, job, member):
            assert (process, job) == (50, 70)
            member._obj.value = state.member
            return True

        def DuplicateHandle(self, process, handle, parent, result, rights, inherit, options):
            assert (process, handle, parent, rights, inherit, options) == (50, 99, 60, 0, False, 2)
            state.duplicates.append("duplicate")
            if not state.duplicate:
                return False
            result._obj.value = 80
            return True

        def CloseHandle(self, handle):
            state.closed.append(handle)

    def convert(handle, flags):
        assert handle == 80
        state.conversions.append(handle)
        return os.dup(source.fileno())

    monkeypatch.setattr(windows, "_api", Kernel)
    monkeypatch.setattr(windows, "_process_identity", lambda api, handle: state.identity)
    monkeypatch.setattr(C, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(os, "O_BINARY", 0, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=convert))
    yield state
    source.close()


def transfer():
    return windows.duplicate_output(42, "windows:100", 99, resident_job=70)


def test_exact_live_member_transfers_output_and_closes_owned_descriptor(boundary, tmp_path):
    with transfer() as output:
        output.write(b"worker output")
        assert boundary.closed == [50]
        descriptor = output.fileno()
    assert output.closed
    with pytest.raises(OSError, match=r".+"):
        os.fstat(descriptor)
    assert (tmp_path / "worker.log").read_bytes() == b"worker output"
    assert boundary.closed == [50]  # Converted handle belongs to CRT, not CloseHandle.


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("identity", "windows:101", "identity changed"),
        ("running", False, "not running"),
        ("member", False, "outside the resident Job"),
    ],
)
def test_reused_exited_or_nonmember_process_never_duplicates(boundary, attribute, value, message):
    setattr(boundary, attribute, value)
    with pytest.raises(OSError, match=message), transfer():
        pytest.fail("unverified stream was yielded")
    assert boundary.duplicates == []
    assert boundary.closed == [50]


def test_duplicate_failure_closes_source_process(boundary):
    boundary.duplicate = False
    with pytest.raises(OSError, match="duplication failed"), transfer():
        pytest.fail("failed duplication was yielded")
    assert boundary.closed == [50]
    assert boundary.conversions == []


def test_crt_failure_closes_untransferred_native_handle(boundary, monkeypatch):
    def fail(*args):
        raise OSError("CRT conversion failed")

    monkeypatch.setattr(sys.modules["msvcrt"], "open_osfhandle", fail)
    with pytest.raises(OSError, match="CRT conversion failed"), transfer():
        pytest.fail("failed conversion was yielded")
    assert boundary.closed == [50, 80]


@pytest.mark.parametrize(
    ("pid", "handle"),
    [
        (0, 1),
        (-1, 1),
        (True, 1),
        (0x100000000, 1),
        (42, 0),
        (42, -1),
        (42, True),
        (42, C.c_void_p(-1).value + 1),
    ],
)
def test_invalid_numeric_owner_is_rejected_before_native_calls(monkeypatch, pid, handle):
    def unexpected():
        pytest.fail("invalid ownership reached Win32")

    monkeypatch.setattr(windows, "_api", unexpected)
    with (
        pytest.raises(ValueError, match="Invalid Windows output owner"),
        windows.duplicate_output(
            pid,
            "windows:100",
            handle,
            resident_job=70,
        ),
    ):
        pytest.fail("invalid ownership was accepted")
