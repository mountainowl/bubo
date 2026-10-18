"""Native installation ownership and Python environment locations."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def environment_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def current_owner() -> int | str:
    """Keep POSIX registration IDs stable; use the process token SID on Windows."""
    return _windows_owner() if sys.platform == "win32" else os.getuid()


def windows_process_identity(pid: int) -> str | None:
    """Creation time of a live process; access failures are not proof of absence."""
    import ctypes
    from ctypes import wintypes

    if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
        raise ValueError("Process identity requires a positive DWORD PID")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # QUERY_LIMITED_INFORMATION | SYNCHRONIZE; never request process mutation rights.
    process = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not process:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: the PID no longer exists.
            return None
        raise OSError(error, "Windows process identity is unavailable")
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(
            process,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(system),
            ctypes.byref(user),
        ):
            raise OSError(ctypes.get_last_error(), "Windows process creation time is unavailable")
        state = kernel.WaitForSingleObject(process, 0)
        if state == 0:  # Signaled process handle: process has exited.
            return None
        if state != 258:  # WAIT_TIMEOUT is the only evidence of a live process.
            raise OSError(ctypes.get_last_error(), "Windows process liveness is unavailable")
        return f"windows:{(created.dwHighDateTime << 32) | created.dwLowDateTime}"
    finally:
        kernel.CloseHandle(process)


def _windows_owner() -> str:
    import ctypes
    from ctypes import wintypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.OpenProcessToken.restype = wintypes.BOOL
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetTokenInformation.restype = wintypes.BOOL
    security.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "Windows installation owner is unavailable")
    try:
        size = wintypes.DWORD()
        # TokenUser=1. The first call obtains the required buffer length.
        if security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size)) or (
            ctypes.get_last_error() != 122
            or size.value < ctypes.sizeof(SidAndAttributes)
            or size.value > 65536
        ):
            raise OSError("Windows process token size is invalid")
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, buffer, size.value, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), "Windows process token is unavailable")
        user = ctypes.cast(buffer, ctypes.POINTER(SidAndAttributes)).contents
        value = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(user.Sid, ctypes.byref(value)):
            raise OSError(ctypes.get_last_error(), "Windows owner SID is unavailable")
        try:
            result = value.value
            if not result or not result.startswith("S-1-"):
                raise OSError("Windows owner SID is invalid")
            return result
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)
