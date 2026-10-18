"""Conservative Windows process inspection before first update registration."""

from __future__ import annotations

import base64
import json
import ntpath
import os
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path

_SNAPSHOT = r"""
$ErrorActionPreference = 'Stop'
$records = @(Get-CimInstance Win32_Process | ForEach-Object {
    [pscustomobject]@{
        pid = [int]$_.ProcessId; parent = [int]$_.ParentProcessId
        name = $_.Name; path = $_.ExecutablePath; command = $_.CommandLine
        created = if ($_.CreationDate) { $_.CreationDate.ToFileTimeUtc() } else { 0 }
    }
})
$json = ConvertTo-Json -InputObject $records -Compress
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json))
"""


def _snapshot() -> dict[int, dict]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SNAPSHOT],
        capture_output=True,
        timeout=20,
    )
    if result.returncode:
        raise OSError("Windows process inspection is unavailable")
    rows = json.loads(base64.b64decode(result.stdout.strip(), validate=True))
    if not isinstance(rows, list):
        raise OSError("Windows process snapshot is invalid")
    processes = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("pid"), int):
            raise OSError("Windows process snapshot is invalid")
        if row["pid"] in processes:
            raise OSError("Windows process snapshot changed")
        processes[row["pid"]] = row
    return processes


def _arguments(command: str) -> list[str]:
    import ctypes
    from ctypes import wintypes

    shell = ctypes.WinDLL("shell32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    arguments = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not arguments:
        raise OSError("Windows invocation arguments are unavailable")
    try:
        return [arguments[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(arguments)


@contextmanager
def _pin(row: dict):
    from bubo.update_windows_process import _api, _process_identity

    api = _api()
    process = api.OpenProcess(0x00101000, False, row["pid"])
    if not process:
        raise OSError("Windows invocation ownership is unavailable")
    try:
        created = int(_process_identity(api, process).removeprefix("windows:"))
        # CIM reports microseconds; the native handle retains 100 ns precision.
        if created // 10 != int(row["created"]) // 10 or api.WaitForSingleObject(process, 0) != 258:
            raise OSError("Windows invocation ownership changed")
        yield
    finally:
        api.CloseHandle(process)


def check_legacy_processes(environment: Path, source: Path | None = None) -> None:
    """Called while the exclusive bootstrap bridge prevents lease-aware starts."""
    rows = _snapshot()
    current = rows.get(os.getpid())
    if current is None:
        raise OSError("Windows configuring process is absent from its snapshot")
    root = ntpath.normcase(str(environment.resolve())).rstrip("\\") + "\\"
    roots = [root]
    if source is not None:
        roots.append(ntpath.normcase(str(source.resolve())).rstrip("\\") + "\\")

    with ExitStack() as handles:
        handles.enter_context(_pin(current))
        exempt = {current["pid"]}
        # Only exact console/venv redirectors of this invocation qualify, not
        # arbitrary ancestors or every process in the caller's desktop session.
        while (parent := rows.get(current.get("parent"))) is not None:
            executable = ntpath.normcase(parent.get("path") or "")
            if not executable.startswith(root) or ntpath.basename(executable) not in {
                "python.exe",
                "pythonw.exe",
                "bubo.exe",
            }:
                break
            if parent["pid"] in exempt or int(parent["created"]) > int(current["created"]):
                raise OSError("Windows invocation ancestry changed")
            parent_args = _arguments(parent.get("command") or "")
            child_args = _arguments(current.get("command") or "")
            child_tail = child_args[1:]
            if child_tail and ntpath.normcase(child_tail[0]) == executable:
                child_tail = child_tail[1:]
            if not parent_args or child_tail != parent_args[1:]:
                break
            handles.enter_context(_pin(parent))
            exempt.add(parent["pid"])
            current = parent

        for pid, row in rows.items():
            if pid in exempt:
                continue
            executable = ntpath.normcase(row.get("path") or "")
            command = ntpath.normcase(row.get("command") or "")
            name = str(row.get("name") or "").lower()
            possible_python = name.startswith(("python", "bubo")) and name.endswith(".exe")
            if possible_python and (not executable or not command):
                raise OSError("A possible legacy Bubo process cannot be inspected")
            if any(executable.startswith(prefix) or prefix in command for prefix in roots):
                raise OSError("Close legacy Bubo processes before registering automatic updates")
