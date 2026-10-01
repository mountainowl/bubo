"""Portable, file-backed subscription circuit breaker.

The monitor is the only writer of ``subscription-circuit.json``.  Pollers and
workers only read it; a worker records a subscription failure by appending a
small, non-sensitive signal file.  This makes the safety decision durable
across worker processes without giving workers authority to reopen the circuit.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

try:  # Windows has no fcntl; the service PID lock remains a safe fallback.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]


DEFAULT_ERROR_PATTERNS = (
    r"insufficient[_ -]?quota",
    r"exceeded your current quota",
    r"quota (?:has been )?(?:exceeded|exhausted|reached)",
    r"subscription (?:has been )?(?:exhausted|expired|inactive|limit)",
    r"plan (?:limit|quota) (?:has been )?(?:reached|exceeded)",
)
_EXCLUDED_ERROR_WORDS = re.compile(
    r"\b(auth|unauthori[sz]ed|forbidden|permission|network|connection|dns|"
    r"certificate|rate[ -]?limit|timeout)\b",
    re.IGNORECASE,
)
_HIGH_CONFIDENCE_QUOTA = re.compile(
    r"\b(?:insufficient[_ -]?quota|exceeded your current quota|"
    r"quota (?:has been )?(?:exceeded|exhausted|reached))\b",
    re.IGNORECASE,
)
_TERMINAL_ERROR = re.compile(r"(?im)^(?:error|fatal|exception)\s*[:=-].*$")
_STRUCTURED_ERROR = re.compile(
    r'(?is)\{\s*"error"\s*:|\{\s*"(?:type|kind)"\s*:\s*"error"'
)


@dataclass(frozen=True)
class CircuitConfig:
    enabled: bool = False
    monitor_interval_seconds: int = 15
    heartbeat_ttl_seconds: int = 60
    probe_interval_seconds: int = 300
    probe_backoff_max_seconds: int = 3600
    probe_timeout_seconds: int = 30
    error_patterns: tuple[str, ...] = DEFAULT_ERROR_PATTERNS


@dataclass(frozen=True)
class CircuitState:
    status: str
    heartbeat_at: float
    opened_at: float | None = None
    next_probe_at: float | None = None
    probe_attempts: int = 0
    version: int = 1

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "status": self.status,
            "heartbeat_at": self.heartbeat_at,
            "opened_at": self.opened_at,
            "next_probe_at": self.next_probe_at,
            "probe_attempts": self.probe_attempts,
        }


def state_path(state_dir: Path) -> Path:
    return state_dir / "subscription-circuit.json"


def signal_path(state_dir: Path) -> Path:
    return state_dir / "subscription-circuit.signal"


def lock_path(state_dir: Path) -> Path:
    return state_dir / "subscription-monitor.lock"


def read_state(path: Path) -> CircuitState | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("status") not in {"closed", "open"}:
            return None
        heartbeat = float(raw["heartbeat_at"])
        return CircuitState(
            status=str(raw["status"]), heartbeat_at=heartbeat,
            opened_at=float(raw["opened_at"]) if raw.get("opened_at") is not None else None,
            next_probe_at=(
                float(raw["next_probe_at"])
                if raw.get("next_probe_at") is not None
                else None
            ),
            probe_attempts=int(raw.get("probe_attempts", 0)),
            version=int(raw.get("version", 1)),
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def write_state(path: Path, state: CircuitState) -> None:
    """Crash-safe JSON write: fsync the file, replace, then fsync directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state.as_dict(), handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:  # directory fsync is not available on every platform
            pass
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def review_gate(cfg: CircuitConfig, path: Path, *, at: float | None = None) -> tuple[bool, str]:
    """Return whether work is safe. Enabled monitoring always fails closed."""
    if not cfg.enabled:
        return True, "disabled"
    if has_failure_signal(signal_path(path.parent)):
        return False, "subscription_signal_pending"
    current = time.time() if at is None else at
    state = read_state(path)
    if state is None:
        return False, "monitor_missing"
    if current - state.heartbeat_at > cfg.heartbeat_ttl_seconds:
        return False, "monitor_stale"
    if state.status != "closed":
        return False, "paused_subscription"
    return True, "closed"


def signal_subscription_failure(path: Path) -> None:
    """Signal only; this deliberately never writes circuit state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, b"1\n")
        os.fsync(fd)
    finally:
        os.close(fd)


def has_failure_signal(path: Path) -> bool:
    """A signal is fail-closed until the monitor durably records ``open``."""
    for candidate in (path, path.with_suffix(path.suffix + ".processing")):
        try:
            if candidate.stat().st_size > 0:
                return True
        except FileNotFoundError:
            continue
    return False


def consume_failure_signal(path: Path) -> bool:
    """Claim signals without acknowledging them before the state write.

    A concurrent writer appends to a new ``.signal`` after ``replace``.  The
    processing file is retained across crashes and only removed by
    :func:`acknowledge_failure_signal` after the monitor has atomically
    persisted an open state.
    """
    processing = path.with_suffix(path.suffix + ".processing")
    if processing.exists():
        try:
            return processing.stat().st_size > 0
        except OSError:
            return True
    try:
        os.replace(path, processing)
    except FileNotFoundError:
        return False
    try:
        return processing.stat().st_size > 0
    except OSError:
        return True


def acknowledge_failure_signal(path: Path) -> None:
    """Acknowledge only after a durable ``status=open`` state transition."""
    with suppress(FileNotFoundError):
        path.with_suffix(path.suffix + ".processing").unlink()


def terminal_error_segment(text: str, *, limit: int = 4096) -> str | None:
    """Return a bounded terminal CLI error/result segment, never source prose."""
    bounded = text[-limit:]
    matches = list(_TERMINAL_ERROR.finditer(bounded))
    if matches:
        return bounded[matches[-1].start() :]
    structured = list(_STRUCTURED_ERROR.finditer(bounded))
    if structured:
        return bounded[structured[-1].start() :]
    return None


def is_subscription_failure(
    text: str,
    patterns: tuple[str, ...] = DEFAULT_ERROR_PATTERNS,
    *,
    terminal_only: bool = False,
) -> bool:
    """Strict classifier: never treat auth, network, or generic billing as quota."""
    candidate = terminal_error_segment(text) if terminal_only else text
    if not candidate:
        return False
    try:
        # Explicit quota codes outrank incidental prior timeout/network text.
        if _HIGH_CONFIDENCE_QUOTA.search(candidate):
            return True
        if _EXCLUDED_ERROR_WORDS.search(candidate):
            return False
        if any(re.search(pattern, candidate, re.IGNORECASE) for pattern in patterns):
            return True
    except re.error:
        return False
    return False


@contextmanager
def singleton_lock(path: Path) -> Iterator[bool]:
    """Best-effort portable singleton lease held for the monitor lifetime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    acquired = False
    try:
        if fcntl is not None:
            handle = path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        else:  # Atomic create is portable; PID/service status clears stale locks.
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                handle = os.fdopen(fd, "w", encoding="utf-8")
                handle.write(str(os.getpid()))
                handle.flush()
                acquired = True
            except FileExistsError:
                try:
                    previous_pid = int(path.read_text(encoding="utf-8").strip())
                    os.kill(previous_pid, 0)
                except (OSError, ValueError):
                    with suppress(FileNotFoundError):
                        path.unlink()
                    try:
                        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        handle = os.fdopen(fd, "w", encoding="utf-8")
                        handle.write(str(os.getpid()))
                        handle.flush()
                        acquired = True
                    except FileExistsError:
                        acquired = False
                else:
                    acquired = False
        yield acquired
    finally:
        if acquired and fcntl is not None and handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if handle is not None:
            handle.close()
        if acquired and fcntl is None:
            with suppress(FileNotFoundError):
                path.unlink()
