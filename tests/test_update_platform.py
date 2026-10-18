import ctypes
import json
import os
import sys
from types import SimpleNamespace

import pytest

from bubo import update_helper, update_platform
from bubo.update_installers import PythonInstaller, UnsafeInstallation


def windows_token(monkeypatch, failure=None):
    from ctypes import wintypes

    events = []
    sid = ctypes.create_unicode_buffer("S-1-5-21-123-456-789-1001")

    def current():
        return 2**40

    def open_token(process, rights, token):
        assert process == 2**40
        assert rights == 8
        token._obj.value = 2**40 + 1
        events.append("open")
        return failure != "open"

    def information(token, kind, buffer, size, required):
        assert token.value == 2**40 + 1
        assert kind == 1
        events.append("size" if buffer is None else "read")
        required._obj.value = 128
        if buffer is None or failure == "read":
            return False
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = 2**40 + 2
        return True

    def convert(pointer, value):
        assert pointer == 2**40 + 2
        events.append("convert")
        if failure == "convert":
            return False
        ctypes.cast(value, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(sid)
        return True

    def free(pointer):
        assert pointer.value == ctypes.addressof(sid)
        events.append("free")

    def close(token):
        assert token.value == 2**40 + 1
        events.append("close")
        return True

    kernel = SimpleNamespace(GetCurrentProcess=current, CloseHandle=close, LocalFree=free)
    security = SimpleNamespace(
        OpenProcessToken=open_token, GetTokenInformation=information, ConvertSidToStringSidW=convert
    )
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, **_: kernel if name == "kernel32" else security,
        raising=False,
    )
    monkeypatch.setattr(
        ctypes, "get_last_error", lambda: 122 if failure != "size" else 5, raising=False
    )
    return events, security, wintypes


def test_windows_owner_uses_token_sid_with_pointer_sized_handles(monkeypatch):
    events, security, wintypes = windows_token(monkeypatch)
    assert update_platform._windows_owner() == "S-1-5-21-123-456-789-1001"
    assert events == ["open", "size", "read", "convert", "free", "close"]
    assert security.OpenProcessToken.argtypes[0] == wintypes.HANDLE
    assert security.GetTokenInformation.argtypes[0] == wintypes.HANDLE


@pytest.mark.parametrize("failure", ["open", "size", "read", "convert"])
def test_windows_owner_failure_closes_only_acquired_handles(monkeypatch, failure):
    events, _, _ = windows_token(monkeypatch, failure)
    with pytest.raises(OSError, match="Windows"):
        update_platform._windows_owner()
    assert ("close" in events) == (failure != "open")
    assert "free" not in events


def test_posix_owner_registration_is_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert update_platform.current_owner() == os.getuid()


def test_windows_registration_rejects_a_different_sid(tmp_path, monkeypatch):
    monkeypatch.setattr(update_helper, "current_owner", lambda: "S-1-5-21-1")
    path = tmp_path / "installation.json"
    path.write_text(json.dumps({"protocol_version": 1, "owner": "S-1-5-21-1"}))
    assert update_helper._read(tmp_path)["owner"] == "S-1-5-21-1"
    monkeypatch.setattr(update_helper, "current_owner", lambda: "S-1-5-21-2")
    with pytest.raises(UnsafeInstallation, match="owner"):
        update_helper._read(tmp_path)


@pytest.mark.parametrize(
    ("platform", "relative"),
    [("win32", "Scripts/python.exe"), ("linux", "bin/python"), ("darwin", "bin/python")],
)
def test_environment_interpreter_matches_platform(tmp_path, monkeypatch, platform, relative):
    monkeypatch.setattr(sys, "platform", platform)
    assert (
        PythonInstaller(tmp_path, tmp_path / "controller", executables={}).python
        == tmp_path / relative
    )


def test_windows_retained_helper_uses_direct_python_without_shell_wrapper(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    archive = update_helper._bundle(tmp_path, sys.executable)
    assert not (tmp_path / "bubo-update-helper").exists()
    assert update_helper._command(tmp_path, {"python": sys.executable, "archive": archive}) == [
        sys.executable,
        archive,
        "--installation",
        str(tmp_path),
    ]


@pytest.mark.parametrize(
    ("error", "state", "expected"),
    [
        (0, 258, "windows:4294967298"),
        (0, 0, None),
        (87, 258, None),
        (5, 258, "error"),
        (0, 0xFFFFFFFF, "error"),
    ],
)
def test_windows_process_identity_distinguishes_absence_from_uncertainty(
    monkeypatch,
    error,
    state,
    expected,
):
    events = []

    def open_process(rights, inherit, pid):
        assert rights == 0x101000
        assert inherit is False
        assert pid == 123
        events.append("open")
        return 0 if error else 2**40

    def times(handle, created, exited, system, user):
        assert handle == 2**40
        created._obj.dwHighDateTime = 1
        created._obj.dwLowDateTime = 2
        return True

    def wait(handle, timeout):
        assert handle == 2**40
        assert timeout == 0
        return state

    def close(handle):
        assert handle == 2**40
        events.append("close")
        return True

    api = SimpleNamespace(
        OpenProcess=open_process, GetProcessTimes=times, WaitForSingleObject=wait, CloseHandle=close
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    if expected == "error":
        with pytest.raises(OSError, match="Windows process"):
            update_platform.windows_process_identity(123)
    else:
        assert update_platform.windows_process_identity(123) == expected
    assert events == (["open"] if error else ["open", "close"])


def test_start_identity_dispatches_to_windows_without_ps(monkeypatch):
    from bubo import update_installers

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update_installers, "windows_process_identity", lambda pid: f"fixture:{pid}")
    assert update_installers._start_identity(123) == "fixture:123"


@pytest.mark.parametrize("pid", [True, None, "123", 0, -1, 2**32 + 123])
def test_windows_process_rejects_invalid_or_truncated_pid_before_api(monkeypatch, pid):
    def unexpected_api(*args, **kwargs):
        raise AssertionError("Invalid PID must not reach Windows API")

    monkeypatch.setattr(ctypes, "WinDLL", unexpected_api, raising=False)
    with pytest.raises(ValueError, match="DWORD PID"):
        update_platform.windows_process_identity(pid)
