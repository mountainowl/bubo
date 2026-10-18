"""Nonblocking OS file locks shared by admission and persistent identity."""

from __future__ import annotations

import errno

try:
    import fcntl
except ImportError:
    fcntl = None


def lock_file(descriptor: int, *, exclusive: bool) -> None:
    """Acquire until the descriptor closes; contention never blocks user work."""
    if fcntl is not None:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        return
    _windows_lock(descriptor, exclusive=exclusive)


def _windows_lock(descriptor: int, *, exclusive: bool, offset: int = 0) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    api.LockFileEx.restype = wintypes.BOOL
    position = Overlapped()
    position.Offset = offset & 0xFFFFFFFF
    position.OffsetHigh = offset >> 32
    # FAIL_IMMEDIATELY, plus EXCLUSIVE_LOCK.
    if not api.LockFileEx(
        msvcrt.get_osfhandle(descriptor),
        1 | (2 if exclusive else 0),
        0,
        1,
        0,
        ctypes.byref(position),
    ):
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError(errno.EAGAIN, "Bubo activity lock is held")
        raise OSError(error, "Windows file locking failed")


def open_windows_environment_lease(configuration, *, exclusive: bool):
    """One bridge protocol for updater, resident and detached-worker admission."""
    import os

    from bubo.update_windows_files import open_environment_file

    descriptor = open_environment_file(configuration)
    try:
        # The reserved byte does not extend or alter the configuration contents.
        _windows_lock(descriptor, exclusive=exclusive, offset=1 << 62)
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        return stream
    finally:
        if descriptor is not None:
            os.close(descriptor)
