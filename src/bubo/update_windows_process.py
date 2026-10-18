"""Windows children assigned atomically to a kill-on-close Job Object.

Requires Windows 10 / Server 2016 (PROC_THREAD_ATTRIBUTE_JOB_LIST). Job names can
be journaled before spawning; they contain no credentials. Windows destroys a
kill-on-close job only after all associated processes terminate, so absence of a
previously owned named job is evidence that its descendants have stopped.
https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
"""

from __future__ import annotations

import ctypes as C
import os
import re
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

DWORD = C.c_uint32
BOOL = C.c_int32
HANDLE = C.c_void_p
SIZE = C.c_size_t


class _FileTime(C.Structure):
    _fields_ = [("low", DWORD), ("high", DWORD)]


class _Limits(C.Structure):
    _fields_ = [
        ("process_time", C.c_int64),
        ("job_time", C.c_int64),
        ("flags", DWORD),
        ("min_working_set", SIZE),
        ("max_working_set", SIZE),
        ("process_limit", DWORD),
        ("affinity", SIZE),
        ("priority", DWORD),
        ("scheduling", DWORD),
    ]


class _IO(C.Structure):
    _fields_ = [
        (key, C.c_uint64)
        for key in (
            "reads",
            "writes",
            "other",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class _ExtendedLimits(C.Structure):
    _fields_ = [
        ("basic", _Limits),
        ("io", _IO),
        ("process_memory", SIZE),
        ("job_memory", SIZE),
        ("peak_process", SIZE),
        ("peak_job", SIZE),
    ]


class _Accounting(C.Structure):
    _fields_ = [(key, C.c_int64) for key in ("user", "kernel", "period_user", "period_kernel")] + [
        (key, DWORD) for key in ("faults", "total", "active", "terminated")
    ]


class _Startup(C.Structure):
    _fields_ = (
        [("cb", DWORD), ("reserved", C.c_wchar_p), ("desktop", C.c_wchar_p), ("title", C.c_wchar_p)]
        + [
            (key, DWORD)
            for key in (
                "x",
                "y",
                "x_size",
                "y_size",
                "x_chars",
                "y_chars",
                "fill",
                "flags",
            )
        ]
        + [
            ("show", C.c_uint16),
            ("reserved_size", C.c_uint16),
            ("reserved_bytes", C.c_void_p),
            ("stdin", HANDLE),
            ("stdout", HANDLE),
            ("stderr", HANDLE),
        ]
    )


class _StartupEx(C.Structure):
    _fields_ = [("startup", _Startup), ("attributes", C.c_void_p)]


class _ProcessInfo(C.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


def _api():
    api = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": (HANDLE, [C.c_void_p, C.c_wchar_p]),
        "OpenProcess": (HANDLE, [DWORD, BOOL, DWORD]),
        "OpenJobObjectW": (HANDLE, [DWORD, BOOL, C.c_wchar_p]),
        "SetInformationJobObject": (BOOL, [HANDLE, C.c_int, C.c_void_p, DWORD]),
        "QueryInformationJobObject": (BOOL, [HANDLE, C.c_int, C.c_void_p, DWORD, C.c_void_p]),
        "TerminateJobObject": (BOOL, [HANDLE, C.c_uint32]),
        "CloseHandle": (BOOL, [HANDLE]),
        "GetCurrentProcess": (HANDLE, []),
        "IsProcessInJob": (BOOL, [HANDLE, HANDLE, C.POINTER(BOOL)]),
        "ExitProcess": (None, [DWORD]),
        "DuplicateHandle": (BOOL, [HANDLE, HANDLE, HANDLE, C.POINTER(HANDLE), DWORD, BOOL, DWORD]),
        "InitializeProcThreadAttributeList": (BOOL, [C.c_void_p, DWORD, DWORD, C.POINTER(SIZE)]),
        "UpdateProcThreadAttribute": (
            BOOL,
            [C.c_void_p, DWORD, SIZE, C.c_void_p, SIZE, C.c_void_p, C.c_void_p],
        ),
        "DeleteProcThreadAttributeList": (None, [C.c_void_p]),
        "CreateProcessW": (
            BOOL,
            [
                C.c_wchar_p,
                C.c_wchar_p,
                C.c_void_p,
                C.c_void_p,
                BOOL,
                DWORD,
                C.c_void_p,
                C.c_wchar_p,
                C.POINTER(_StartupEx),
                C.POINTER(_ProcessInfo),
            ],
        ),
        "WaitForSingleObject": (DWORD, [HANDLE, DWORD]),
        "GetExitCodeProcess": (BOOL, [HANDLE, C.POINTER(DWORD)]),
        "GetProcessTimes": (BOOL, [HANDLE, *([C.POINTER(_FileTime)] * 4)]),
    }
    for name, (restype, argtypes) in signatures.items():
        function = getattr(api, name)
        function.restype, function.argtypes = restype, argtypes
    return api


def _error(action):
    return OSError(C.get_last_error(), "Windows update process " + action)


def _process_identity(api, process):
    created, exited, system, user = (_FileTime() for _ in range(4))
    if not api.GetProcessTimes(
        process,
        C.byref(created),
        C.byref(exited),
        C.byref(system),
        C.byref(user),
    ):
        raise _error("child creation identity is unavailable")
    return f"windows:{(created.high << 32) | created.low}"


@contextmanager
def duplicate_output(pid: int, identity: str, handle: int, *, resident_job):
    """Borrow a launcher's output stream using its exact live process identity.

    The retained supervisor uses this for sibling worker launches. The caller
    must authenticate the request and keep its stream open until acknowledgment.
    Never duplicate by PID alone: retain the verified source process handle for
    the complete operation. Neither handle is made inheritable here.
    """
    if (
        type(pid) is not int
        or not 0 < pid <= 0xFFFFFFFF
        or type(handle) is not int
        or not 0 < handle <= C.c_void_p(-1).value
    ):
        raise ValueError("Invalid Windows output owner or handle")
    if not resident_job:
        raise ValueError("Resident Job ownership is required")
    import msvcrt

    api = _api()
    process = api.OpenProcess(0x1000 | 0x0040 | 0x100000, False, pid)
    if not process:
        raise _error("output owner is unavailable")
    duplicate = HANDLE()
    try:
        if _process_identity(api, process) != identity:
            raise OSError("Windows output owner identity changed")
        if api.WaitForSingleObject(process, 0) != 258:
            raise OSError("Windows output owner is not running")
        member = BOOL()
        if not api.IsProcessInJob(process, resident_job, C.byref(member)) or not member.value:
            raise OSError("Windows output owner is outside the resident Job")
        if not api.DuplicateHandle(
            process,
            handle,
            api.GetCurrentProcess(),
            C.byref(duplicate),
            0,
            False,
            2,
        ):
            raise _error("output duplication failed")
    finally:
        api.CloseHandle(process)
    try:
        descriptor = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(duplicate.value)
        raise
    # open_osfhandle transfers ownership to the CRT descriptor.
    try:
        stream = os.fdopen(descriptor, "wb", buffering=0)
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


@contextmanager
def verified_job_member(pid: int, identity: str, job):
    """Pin and verify a live member, including a venv redirector's interpreter."""
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF or not job:
        raise ValueError("Invalid resident process ownership")
    api = _api()
    process = api.OpenProcess(0x1000 | 0x100000, False, pid)
    if not process:
        raise _error("resident member is unavailable")
    try:
        member = BOOL()
        if (
            _process_identity(api, process) != identity
            or api.WaitForSingleObject(process, 0) != 258
            or not api.IsProcessInJob(process, job, C.byref(member))
            or not member.value
        ):
            raise OSError("Resident process identity or Job membership changed")
        yield
    finally:
        api.CloseHandle(process)


def new_job_name() -> str:
    return "Global\\BuboUpdate-" + uuid.uuid4().hex


def _name(name):
    if not isinstance(name, str) or not re.fullmatch(r"Global\\BuboUpdate-[0-9a-f]{32}", name):
        raise ValueError("Invalid Bubo process job name")
    return name


def _active(api, job):
    accounting = _Accounting()
    if not api.QueryInformationJobObject(job, 1, C.byref(accounting), C.sizeof(accounting), None):
        raise _error("membership is unavailable")
    return accounting.active


def _wait_empty(api, job, timeout):
    deadline = time.monotonic() + timeout
    while _active(api, job):
        if time.monotonic() >= deadline:
            raise TimeoutError("Windows update descendants did not stop")
        time.sleep(0.02)


def _member_handles(api, job):
    """Retain members while Job accounting and native process exit converge."""
    capacity = 64
    while True:
        buffer = C.create_string_buffer(8 + capacity * C.sizeof(SIZE))
        if api.QueryInformationJobObject(job, 3, buffer, len(buffer), None):
            count = DWORD.from_buffer(buffer, 4).value
            if count > capacity:
                raise OSError("Windows Job process list changed unexpectedly")
            break
        if C.get_last_error() != 234 or capacity >= 65536:
            raise _error("Job member list is unavailable")
        capacity *= 2
    handles = []
    try:
        for index in range(count):
            pid = SIZE.from_buffer(buffer, 8 + index * C.sizeof(SIZE)).value
            handle = api.OpenProcess(0x1000 | 0x100000, False, pid)
            if not handle:
                if C.get_last_error() == 87:
                    continue  # Process already exited and was removed.
                raise _error("Job member could not be retained")
            handles.append(handle)
            member = BOOL()
            if not api.IsProcessInJob(handle, job, C.byref(member)) or not member.value:
                api.CloseHandle(handles.pop())
        return handles
    except BaseException:
        for handle in handles:
            api.CloseHandle(handle)
        raise


def _terminate_job(api, job, timeout):
    handles = _member_handles(api, job)
    try:
        if not api.TerminateJobObject(job, 1):
            raise _error("job termination failed")
        deadline = time.monotonic() + timeout
        for handle in handles:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if api.WaitForSingleObject(handle, remaining) != 0:
                raise TimeoutError("Windows Job member has not finished exiting")
        _wait_empty(api, job, max(0, deadline - time.monotonic()))
    finally:
        for handle in handles:
            api.CloseHandle(handle)


def job_quiescent(name: str) -> bool:
    """Read-only membership proof; never stop a concurrently adopting guardian."""
    api = _api()
    job = api.OpenJobObjectW(0x0004, False, _name(name))
    if not job:
        if C.get_last_error() == 2:
            return True
        raise _error("job membership is unavailable")
    try:
        return _active(api, job) == 0
    finally:
        api.CloseHandle(job)


def recover_job(name: str, *, timeout: float = 5) -> None:
    """Caller owns the abandoned launch, or proved its journaled parent is gone."""
    name = _name(name)
    api = _api()
    job = api.OpenJobObjectW(0x0004 | 0x0008, False, name)  # QUERY | TERMINATE
    if not job:
        if C.get_last_error() == 2:  # Job was destroyed after its processes exited.
            return
        raise _error("job recovery is unavailable")
    try:
        _terminate_job(api, job, timeout)
    finally:
        api.CloseHandle(job)


class GuardianJob:
    """Retain an existing Job from inside its single-threaded worker guardian.

    Open before acknowledging handoff. Hold admission leases until native exit;
    neither context-manager cleanup nor application atexit callbacks may run after
    publishing retirement. Opening a handle alone does not transfer ledger ownership.
    """

    def __init__(self, name: str):
        self.name = _name(name)
        self.api = api = _api()
        self.job = None
        job = api.OpenJobObjectW(0x0004, False, self.name)  # QUERY, non-inheritable
        if not job:
            raise _error("guardian job is unavailable")
        try:
            member = BOOL()
            if not api.IsProcessInJob(api.GetCurrentProcess(), job, C.byref(member)):
                raise _error("guardian membership is unavailable")
            if not member.value:
                raise OSError("Windows guardian is outside its recorded job")
            limits = _ExtendedLimits()
            if not api.QueryInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits), None):
                raise _error("guardian job limits are unavailable")
            flags = limits.basic.flags
            if not flags & 0x2000 or flags & (0x800 | 0x1000):
                raise OSError("Windows guardian job permits uncontained descendants")
        except BaseException:
            api.CloseHandle(job)
            raise
        self.job = job

    def alone(self):
        """Valid only after the guardian has irrevocably stopped spawning work."""
        member = BOOL()
        if not self.api.IsProcessInJob(self.api.GetCurrentProcess(), self.job, C.byref(member)):
            raise _error("guardian membership is unavailable")
        if not member.value:
            raise OSError("Windows guardian job membership changed")
        return _active(self.api, self.job) == 1

    def exit(self, status: int):
        """Keep the Job and all admission leases held through immediate OS exit."""
        self.api.ExitProcess(status)
        raise RuntimeError("Windows ExitProcess unexpectedly returned")

    def __del__(self):
        # The guardian must normally use exit(), retaining leases until OS exit.
        job = getattr(self, "job", None)
        if job:
            self.api.CloseHandle(job)
            self.job = None


def run_capture(
    args: list[str],
    *,
    name: str,
    env: dict[str, str],
    timeout: float,
    on_started=None,
    on_job_created=None,
) -> bytes:
    """Installer output stays in a temporary file; no pipe can deadlock a child tree."""
    import msvcrt

    with (
        tempfile.TemporaryFile() as output,
        open(os.devnull, "rb") as stdin,
        open(os.devnull, "wb") as stderr,
    ):
        child = JobProcess(
            args,
            name=name,
            env=env,
            on_job_created=on_job_created,
            handles=tuple(
                msvcrt.get_osfhandle(stream.fileno()) for stream in (stdin, output, stderr)
            ),
        )
        try:
            if on_started:
                on_started(child.pid)
            status = child.wait(timeout=timeout)
            # Root exit alone is not permission to migrate or restore shared state.
            _wait_empty(child.api, child.job, 5)
            if status:
                raise OSError("Update subprocess failed")
            output.seek(0)
            return output.read()
        finally:
            child.close()


class JobProcess:
    """Own a process tree; supplied stdio handles are duplicated, never consumed."""

    def __init__(
        self,
        args: list[str],
        *,
        name: str,
        handles: tuple[int, int, int],
        env: dict[str, str],
        cwd: str | None = None,
        on_job_created=None,
    ):
        self.name = _name(name)
        if not args or not Path(args[0]).is_absolute() or any("\0" in arg for arg in args):
            raise ValueError("Windows update executable must be an absolute path")
        if len(handles) != 3 or any(
            type(handle) is not int or not 0 < handle < (1 << (8 * C.sizeof(HANDLE))) - 1
            for handle in handles
        ):
            raise ValueError("Windows update requires three valid stdio handles")
        if any(
            not isinstance(key, str)
            or not key
            or "=" in key
            or "\0" in key
            or not isinstance(value, str)
            or "\0" in value
            for key, value in env.items()
        ):
            raise ValueError("Invalid Windows update environment")
        self.api = api = _api()
        self.job = self.process = None
        self.returncode = None
        self.args = args
        C.set_last_error(0)
        job = api.CreateJobObjectW(None, self.name)
        if not job:
            raise _error("job creation failed")
        if C.get_last_error() == 183:
            api.CloseHandle(job)
            raise OSError("Refusing to adopt an existing Windows process job")
        self.job = job
        duplicates = []
        attributes = None
        initialized = False
        try:
            limits = _ExtendedLimits()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway allowed.
            if not api.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)):
                raise _error("job limits failed")
            if on_job_created:
                on_job_created()
            parent = api.GetCurrentProcess()
            for handle in handles:
                duplicate = HANDLE()
                if not api.DuplicateHandle(parent, handle, parent, C.byref(duplicate), 0, True, 2):
                    raise _error("stdio duplication failed")
                duplicates.append(duplicate.value)
            size = SIZE()
            if api.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size)) or (
                C.get_last_error() != 122 or not 0 < size.value <= 1024 * 1024
            ):
                raise _error("attribute sizing failed")
            attributes = C.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)):
                raise _error("attribute initialization failed")
            initialized = True
            jobs = (HANDLE * 1)(job)
            inherited = (HANDLE * 3)(*duplicates)
            for key, value in ((0x2000D, jobs), (0x20002, inherited)):
                if not api.UpdateProcThreadAttribute(
                    attributes,
                    0,
                    key,
                    value,
                    C.sizeof(value),
                    None,
                    None,
                ):
                    raise _error("atomic child ownership is unsupported")
            startup = _StartupEx()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = duplicates
            startup.attributes = C.cast(attributes, C.c_void_p)
            command = C.create_unicode_buffer(subprocess.list2cmdline(args))
            environment = C.create_unicode_buffer(
                "\0".join(
                    f"{key}={value}"
                    for key, value in sorted(env.items(), key=lambda pair: pair[0].casefold())
                )
                + "\0\0"
            )
            info = _ProcessInfo()
            # Assign to the job atomically during creation, never spawn-then-assign.
            if not api.CreateProcessW(
                args[0],
                command,
                None,
                None,
                True,
                0x00080000 | 0x00000400 | 0x00000008,  # EXTENDED | UNICODE_ENV | DETACHED
                environment,
                cwd,
                C.byref(startup),
                C.byref(info),
            ):
                raise _error("child creation failed")
            self.process, self.pid = info.process, info.pid
            api.CloseHandle(info.thread)
        except BaseException:
            if self.process:
                api.CloseHandle(self.process)
                self.process = None
            api.CloseHandle(self.job)
            self.job = None
            raise
        finally:
            if initialized:
                api.DeleteProcThreadAttributeList(attributes)
            for duplicate in duplicates:
                api.CloseHandle(duplicate)

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        status = self.api.WaitForSingleObject(self.process, 0)
        if status == 258:
            return None
        if status != 0:
            raise _error("wait failed")
        result = DWORD()
        if not self.api.GetExitCodeProcess(self.process, C.byref(result)):
            raise _error("exit status failed")
        self.returncode = result.value
        return self.returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(0.02)
        return self.returncode

    def terminate(self, *, timeout=5):
        _terminate_job(self.api, self.job, timeout)
        self.wait(timeout=timeout)

    def quiescent(self):
        return _active(self.api, self.job) == 0

    def identity(self):
        """Creation identity from our retained handle, including after child exit."""
        if not self.process:
            raise OSError("Windows child process handle was relinquished")
        return _process_identity(self.api, self.process)

    def detach(self, acknowledge):
        """Relinquish handles only after the caller validates durable handoff.

        The acknowledgement must prove this exact child owns the ledger, holds its
        own Job handle and admission leases. Failed validation preserves ordinary
        close/termination behavior. No external code runs between validation and
        relinquishment.
        """
        if not self.job or not self.process:
            raise OSError("Windows child ownership was already relinquished")
        acknowledge(self)
        if not self.api.CloseHandle(self.job):
            raise _error("job handoff handle close failed")
        self.job = None
        # Handoff is committed. A process-handle cleanup failure must not invite
        # a duplicate launch; retain it for close/destructor without kill authority.
        if self.api.CloseHandle(self.process):
            self.process = None

    def retain_members(self):
        """Pin resident members before cooperative shutdown starts."""
        if not hasattr(self, "_stop_handles"):
            self._stop_handles = _member_handles(self.api, self.job)

    def close(self):
        if self.job:
            if not self.quiescent():
                self.terminate()
            # Job accounting can reach zero before process exit is signaled.
            # Keep ownership on timeout so callers cannot publish a false stop.
            deadline = time.monotonic() + 5
            handles = ([self.process] if self.process else []) + list(
                getattr(self, "_stop_handles", ())
            )
            for handle in handles:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if self.api.WaitForSingleObject(handle, remaining) != 0:
                    raise TimeoutError("Windows Job member has not finished exiting")
            for handle in getattr(self, "_stop_handles", ()):
                self.api.CloseHandle(handle)
            self._stop_handles = []
            self.api.CloseHandle(self.job)
            self.job = None
        if self.process:
            self.api.CloseHandle(self.process)
            self.process = None

    def __del__(self):
        # Closing the final job handle kills descendants even during interpreter teardown.
        api = getattr(self, "api", None)
        for handle in getattr(self, "_stop_handles", ()):
            api.CloseHandle(handle)
        self._stop_handles = []
        for name in ("job", "process"):
            handle = getattr(self, name, None)
            if handle:
                api.CloseHandle(handle)
                setattr(self, name, None)
