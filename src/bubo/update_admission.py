"""Nonblocking process leases: the updater yields to work, never the reverse.

Registration creates locks before enabling updates. Unregistered/read-only entry
points never bootstrap state. A persistent fence outlives a killed lock holder.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from functools import partial, wraps
from pathlib import Path

from bubo import paths
from bubo.update_locking import lock_file
from bubo.update_registration import load_registration
from bubo.version import installed_version

UNREGISTERED_STATE_LEASE_VERSION = 2

try:
    import fcntl
except ImportError:
    fcntl = None


class MaintenanceInProgress(RuntimeError):
    """Retryable: a committed update/recovery owns application admission."""


class UpdateBusy(RuntimeError):
    """Normal work owns a lease; the updater must try later without fencing."""


_local = threading.local()
_PROCESS_VERSION = installed_version()


@contextmanager
def _file_lock(path: Path, *, exclusive: bool):
    from types import SimpleNamespace

    # Registration creates lock files. Ordinary operations only open existing paths.
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            lock_file(descriptor, exclusive=exclusive)
        except BlockingIOError as exc:
            raise UpdateBusy from exc
        # Closing preserves a POSIX lease while inherited worker descriptors remain.
        yield SimpleNamespace(fileno=lambda: descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def environment_lock(environment: Path, *, exclusive: bool = False):
    """Bridge first registration using an existing file, without read-only writes."""
    configuration = environment / "pyvenv.cfg"
    if not configuration.is_file():
        yield None
        return
    if sys.platform == "win32":
        from bubo.update_locking import open_windows_environment_lease

        try:
            stream = open_windows_environment_lease(configuration, exclusive=exclusive)
        except BlockingIOError as error:
            raise UpdateBusy from error
        with stream:
            yield stream
        return
    with _file_lock(configuration, exclusive=exclusive) as stream:
        yield stream


@contextmanager
def activity(state: Path | None = None, *, writable: bool = False):
    state = (state or paths.DB.parent).resolve()
    if writable:
        from bubo.update_helper import ensure_registered

        ensure_registered(paths.ROOT, state=state)
    bridge = environment_lock(Path(sys.prefix))
    if sys.platform == "win32":
        from bubo.update_helper import installation_for_environment

        # Registered activity is protected by installation/state admission below.
        # Opening the bridge during replacement would itself block Windows rename.
        installation = installation_for_environment(Path(sys.prefix))
        if (installation / "installation.json").is_file():
            bridge = nullcontext(None)
    try:
        with bridge as stream:
            previous = getattr(_local, "handoff_fds", ())
            _local.handoff_fds = (*previous, stream.fileno()) if stream is not None else previous
            try:
                with _registered_activity(state, writable=writable):
                    yield
            finally:
                _local.handoff_fds = previous
    except UpdateBusy as exc:
        raise MaintenanceInProgress("Bubo maintenance is in progress; retry shortly") from exc


@contextmanager
def _registered_activity(state: Path, *, writable: bool):
    depths = getattr(_local, "depths", None)
    if depths is None:
        depths = _local.depths = {}
    # PID prevents a forked worker from inheriting the parent's reentrancy claim.
    key = (os.getpid(), state)
    if depths.get(key):
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
        return
    registration = state / "update-registration.json"
    if not registration.exists():
        from bubo.update_helper import installation_for_environment

        installation = installation_for_environment(Path(sys.prefix))
        with ExitStack() as locks:
            descriptors = []
            if (installation / "installation.json").exists():
                descriptors.append(
                    locks.enter_context(
                        _file_lock(installation / "activity.lock", exclusive=False)
                    ).fileno()
                )
                _check_generation(installation)
            if state.is_dir() and fcntl is not None:
                descriptors.append(locks.enter_context(_file_lock(state, exclusive=False)).fileno())
            if not registration.exists():
                previous = getattr(_local, "handoff_fds", ())
                _local.handoff_fds = (*previous, *descriptors)
                try:
                    yield
                finally:
                    _local.handoff_fds = previous
                return
        # Release provisional leases before entering the registered lock order.
        with _registered_activity(state, writable=writable):
            yield
        return
    try:
        record = load_registration(state)
        installation = Path(record["installation"])
        with _file_lock(installation / "activity.lock", exclusive=False) as install_lock:
            _check_generation(installation)
            with _file_lock(state / "update-activity.lock", exclusive=False) as state_lock:
                if load_registration(state) != record:
                    raise MaintenanceInProgress("Bubo update registration changed; retry shortly")
                depths[key] = 1
                previous = getattr(_local, "handoff_fds", ())
                _local.handoff_fds = (*previous, install_lock.fileno(), state_lock.fileno())
                try:
                    yield
                finally:
                    _local.handoff_fds = previous
                    depths.pop(key, None)
    except UpdateBusy as exc:
        raise MaintenanceInProgress("Bubo maintenance is in progress; retry shortly") from exc


def admitted(function=None, *, writable: bool = False):
    """Wrap a complete finite operation, never an idle service/MCP lifetime."""
    if function is None:
        return partial(admitted, writable=writable)

    @wraps(function)
    def wrapper(*args, **kwargs):
        with activity(writable=writable):
            return function(*args, **kwargs)

    return wrapper


def handoff_fds() -> tuple[int, ...]:
    """Keep the parent's lease alive in a detached worker across fork/exec."""
    return () if os.name == "nt" else getattr(_local, "handoff_fds", ())


def _check_generation(installation: Path) -> None:
    if (installation / "maintenance.json").exists():
        raise MaintenanceInProgress("Bubo maintenance is in progress; retry shortly")
    generation = installation / "generation.json"
    if generation.exists():
        version = json.loads(generation.read_text())["version"]
        if version.split("+", 1)[0] != (_PROCESS_VERSION or "").split("+", 1)[0]:
            raise MaintenanceInProgress("Bubo was updated; restart this process")


def restart_required(state: Path | None = None) -> bool:
    """Only a completed update may retire a resident process's loaded code."""
    state = (state or paths.DB.parent).resolve()
    try:
        registration = state / "update-registration.json"
        if registration.exists():
            installation = Path(load_registration(state)["installation"])
        else:
            from bubo.update_helper import installation_for_environment

            installation = installation_for_environment(Path(sys.prefix))
            if not (installation / "installation.json").exists():
                return False
        with _file_lock(installation / "activity.lock", exclusive=False):
            if (installation / "maintenance.json").exists():
                return False
            generation = json.loads((installation / "generation.json").read_text())
            return (
                generation["version"].split("+", 1)[0] != (_PROCESS_VERSION or "").split("+", 1)[0]
            )
    except OSError, ValueError, KeyError, UpdateBusy:
        return False


def restart_process() -> None:
    """Reload a resident using its original invocation and in-memory environment."""
    os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])


@contextmanager
def resident_restart(restart, *, state: Path | None = None, interval: float = 1):
    """Retire stale residents while idle, without writing to the MCP protocol."""
    stop = threading.Event()

    def watch():
        while not stop.wait(interval):
            if restart_required(state):
                restart()
                return

    watcher = threading.Thread(target=watch, name="bubo-update-restart", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(timeout=2)


@contextmanager
def exclusive(installation: Path, states: list[Path]):
    """Try all locks, release partial acquisitions on contention; no busy fence."""
    with ExitStack() as stack:
        stack.enter_context(_file_lock(installation / "registry.lock", exclusive=True))
        stack.enter_context(_file_lock(installation / "activity.lock", exclusive=True))
        for state in sorted({state.resolve() for state in states}):
            stack.enter_context(_file_lock(state / "update-activity.lock", exclusive=True))
        if sys.platform == "win32":
            from bubo.update_windows_jobs import JobsBusy, drain

            try:
                drain(installation)
            except JobsBusy as error:
                raise UpdateBusy from error
        yield
