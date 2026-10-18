"""Persistent identity guard shared by telemetry and interrupted-update recovery."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from bubo.update_locking import lock_file


def sync_directory(path: Path) -> None:
    if sys.platform == "win32":
        from bubo.update_windows_files import flush_directory

        flush_directory(path)
        return
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def atomic_json(path: Path, data: object) -> None:
    """Flush and atomically publish a complete record.

    An error after replacement may leave the new record installed; callers must
    inspect it during recovery rather than assuming the old record survived.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if sys.platform == "win32":
            from bubo.update_windows_files import replace_file

            replace_file(Path(temporary), path)
        else:
            os.replace(temporary, path)
            sync_directory(path.parent)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def guard_path(state: Path) -> Path:
    return state / "update-identity.json"


def read_identity(state: Path) -> str | None:
    path = state / "install_id"
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    if uuid.UUID(value).version != 4:
        raise ValueError("Invalid installation identity")
    return value


def guarded_identity(state: Path) -> str | None:
    """Called while holding install_id.lock; never initializes an identity."""
    record = json.loads(guard_path(state).read_text(encoding="utf-8"))
    if record.get("schema") != 1 or "identity" not in record:
        raise ValueError("Unsupported identity guard")
    identity = read_identity(state)
    if identity != record["identity"]:
        raise ValueError("Installation identity changed during update")
    return identity


@contextmanager
def identity_lock(state: Path):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "install_id.lock").open("a") as lock:
        lock_file(lock.fileno(), exclusive=True)
        yield


def protect_identity(state: Path) -> str | None:
    """Snapshot once. An existing guard must be validated, never overwritten."""
    with identity_lock(state):
        if guard_path(state).exists():
            return guarded_identity(state)
        identity = read_identity(state)
        atomic_json(guard_path(state), {"schema": 1, "identity": identity})
        return identity


def release_identity(state: Path) -> None:
    """Only the updater may release after matched code/DB recovery or commit."""
    with identity_lock(state):
        guarded_identity(state)
        guard_path(state).unlink()
        sync_directory(state)
