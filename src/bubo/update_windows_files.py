"""Native same-directory journal publication; not general directory durability."""

from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import wintypes
from pathlib import Path

_REPLACE_EXISTING = 0x1
_WRITE_THROUGH = 0x8


def _move_file():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move.restype = wintypes.BOOL
    return move


def replace_file(source: Path, destination: Path) -> None:
    """Request a write-through rename of a flushed same-directory temporary file.

    No copy/delete or delayed-reboot fallback is allowed. This does not replace
    directory syncing for environment moves, mkdir, unlink, or rollback sequences.
    Any error leaves the publication outcome uncertain; callers must inspect it.
    """
    source, destination = source.absolute(), destination.absolute()
    if source.parent.resolve() != destination.parent.resolve():
        raise ValueError("Journal replacement must stay in the same directory")
    if not _move_file()(str(source), str(destination), _REPLACE_EXISTING | _WRITE_THROUGH):
        raise ctypes.WinError(ctypes.get_last_error())


def claim_file(path: Path) -> None:
    """Publish a flushed claim once, without replacing an existing claim."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(b"claimed\n")
            stream.flush()
            os.fsync(stream.fileno())
        if not _move_file()(str(temporary), str(path.absolute()), _WRITE_THROUGH):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _file_api():
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    api.CreateFileW.restype = ctypes.c_void_p
    api.FlushFileBuffers.argtypes = [ctypes.c_void_p]
    api.FlushFileBuffers.restype = ctypes.c_int
    api.CloseHandle.argtypes = [ctypes.c_void_p]
    api.CloseHandle.restype = ctypes.c_int
    api.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    api.GetFileInformationByHandleEx.restype = ctypes.c_int
    return api


def flush_directory(path: Path) -> None:
    """Flush an owned directory handle; reject redirected or unsupported paths."""
    api = _file_api()
    # GENERIC_WRITE, SHARE_READ|WRITE|DELETE, OPEN_EXISTING,
    # BACKUP_SEMANTICS|OPEN_REPARSE_POINT. No elevated privileges requested.
    handle = api.CreateFileW(str(path.absolute()), 0x40000000, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = (ctypes.c_uint32 * 2)()
        if not api.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not attributes[0] & 0x10 or attributes[0] & 0x400:
            raise OSError("Windows directory flush requires an ordinary directory")
        if not api.FlushFileBuffers(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        api.CloseHandle(handle)


def open_environment_file(path: Path) -> int:
    """Open an existing bridge lease without preventing environment replacement."""
    import msvcrt

    api = _file_api()
    handle = api.CreateFileW(str(path.absolute()), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = (ctypes.c_uint32 * 2)()
        if not api.GetFileInformationByHandleEx(handle, 9, attributes, ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes[0] & (0x10 | 0x400):
            raise OSError("Windows environment lease requires an ordinary file")
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # Ownership transferred to the CRT descriptor.
        return descriptor
    finally:
        if handle is not None:
            api.CloseHandle(handle)
