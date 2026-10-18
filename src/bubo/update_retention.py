"""Bound completed update work without deleting active or uncertain recovery data."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from bubo.update_admission import _file_lock
from bubo.update_identity import atomic_json, sync_directory


def _mounted_storage(work: Path) -> bool:
    if sys.platform.startswith("linux"):
        # st_dev/ismount alone cannot detect same-filesystem bind mounts.
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            if len(fields) < 6:
                raise OSError("Mount inventory is incomplete")
            mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4])
            if Path(mount).is_relative_to(work):
                return True
        return False
    for directory, children, _ in os.walk(work, followlinks=False):
        if os.path.ismount(directory):
            return True
        children[:] = [name for name in children if not (Path(directory) / name).is_symlink()]
    return False


def remember(journal: dict) -> None:
    """Mirror durable progress for later cleanup; the installation journal is authoritative."""
    work = Path(journal["work"])
    atomic_json(work / "retention.json", {**journal, "retention_updated_ns": time.time_ns()})


def _resume_removal(installation: Path, protected: list[Path], snapshots: list[Path]) -> bool:
    pending = installation / "retention-pending.json"
    if pending.is_symlink():
        raise OSError("Cleanup intent was redirected")
    record = json.loads(pending.read_text())
    identifier = record["attempt_id"]
    if str(uuid.UUID(identifier)) != identifier or type(record["snapshot"]) is not bool:
        raise OSError("Cleanup ownership is invalid")
    work = installation / "attempts" / identifier
    if work.is_symlink():
        raise OSError("Cleanup directory was redirected")
    if work.exists():
        stat = work.stat()
        if (stat.st_dev, stat.st_ino) != (record["device"], record["inode"]):
            raise OSError("Cleanup directory ownership changed")
        if any(path.is_relative_to(work) for path in protected):
            return False
        if record["snapshot"] and not any(path != work for path in snapshots):
            return False
        if _mounted_storage(work):
            raise OSError("Retained update snapshot contains mounted storage")
        shutil.rmtree(work)
    sync_directory(work.parent)
    pending.unlink()
    sync_directory(installation)
    return True


def prune(installation: Path) -> None:
    """Keep the current attempt, newest successful snapshot and two completed staged attempts.

    Abandoned staging needs explicit proof its writers stopped. Unknown ownership
    is preserved. Cleanup never follows symlinks or removes live paths.
    """
    if not shutil.rmtree.avoids_symlink_attacks:
        return
    with (
        _file_lock(installation / "attempt.lock", exclusive=True),
        _file_lock(installation / "registry.lock", exclusive=True),
    ):
        if (installation / "maintenance.json").exists():
            return
        attempts = installation / "attempts"
        if not attempts.exists() or attempts.is_symlink():
            return
        registered = json.loads((installation / "installation.json").read_text())
        protected = [Path(item).resolve() for item in registered.get("states", [])]
        for key in ("environment", "source", "python", "archive"):
            if registered.get(key):
                protected.append(Path(registered[key]).resolve())
        current = installation / "attempt.json"
        if current.exists():
            protected.append(Path(json.loads(current.read_text())["work"]).resolve())
        pending = installation / "retention-pending.json"
        pending_id = None
        if pending.is_symlink():
            raise OSError("Cleanup intent was redirected")
        if pending.exists():
            pending_id = json.loads(pending.read_text())["attempt_id"]
        completed, staged, abandoned = [], [], []
        for work in attempts.iterdir():
            if work.is_symlink() or not work.is_dir() or work.name == pending_id:
                continue
            try:
                if str(uuid.UUID(work.name)) != work.name:
                    continue
                marker = work / "retention.json"
                if marker.is_symlink():
                    continue
                record = json.loads(marker.read_text())
                if (
                    record["attempt_id"] != work.name
                    or Path(record["work"]) != work
                    or type(record["retention_updated_ns"]) is not int
                ):
                    continue
                entry = (record["retention_updated_ns"], work)
                if record["phase"] in {"committed", "restored"}:
                    completed.append(entry)
                elif (
                    record["phase"] in {"deferred", "failed"}
                    and record.get("staged") is True
                    and not record.get("mutated")
                ):
                    staged.append(entry)
                elif (
                    record["phase"] in {"staging", "failed"}
                    and record.get("staged") is False
                    and record.get("mutated") is False
                ):
                    abandoned.append(entry)
            except OSError, ValueError, KeyError, TypeError:
                continue
        snapshots = [work for _, work in completed]
        if pending.exists() and not _resume_removal(installation, protected, snapshots):
            return
        for entries, keep in ((completed, 1), (staged, 2), (abandoned, 0)):
            entries.sort(reverse=True)
            for _, work in entries[keep:]:
                if any(path.is_relative_to(work) for path in protected):
                    continue
                if _mounted_storage(work):
                    raise OSError("Retained update snapshot contains mounted storage")
                if entries is abandoned:
                    from bubo.update_installers import writer_quiescent

                    writer = work / "stage-writer.json"
                    try:
                        if (
                            not writer.is_file()
                            or writer.is_symlink()
                            or not writer_quiescent(writer)
                        ):
                            continue
                    except OSError, ValueError, KeyError, TypeError:
                        continue
                stat = work.stat()
                atomic_json(
                    pending,
                    {
                        "attempt_id": work.name,
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                        "snapshot": entries is completed,
                    },
                )
                if not _resume_removal(installation, protected, snapshots):
                    return
                snapshots = [item for item in snapshots if item != work]
