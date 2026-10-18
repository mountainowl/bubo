"""Cooperative service-stop publication shared with the retained updater."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def stop_path(pid_path: Path, token: str) -> Path:
    return pid_path.with_suffix(f".stop.{hashlib.sha256(token.encode('utf-8')).hexdigest()}")


def publish_stop(pid_path: Path, token: str) -> None:
    """Publish the complete generation token before a watcher can consume it."""
    target = stop_path(pid_path, token)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=target.name + ".",
        delete=False,
    ) as stream:
        temporary = type(target)(stream.name)
        try:
            stream.write(token)
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
